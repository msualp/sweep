"""
on_comment is responsible for handling PR comments and PR review comments, called from sweepai/api.py.
It is also called in sweepai/handlers/on_ticket.py when Sweep is reviewing its own PRs.
"""
import copy
import re
import time
import traceback
from typing import Any

from loguru import logger
from tabulate import tabulate

from sweepai.config.client import get_documentation_dict
from sweepai.config.server import (
    DEFAULT_GPT4_MODEL,
    ENV,
    GITHUB_BOT_USERNAME,
    MONGODB_URI,
)
from sweepai.core.entities import MockPR, NoFilesException
from sweepai.core.sweep_bot import SweepBot, get_files_to_change, validate_file_change_requests
from sweepai.handlers.create_pr import handle_file_change_requests
from sweepai.core.review_utils import get_pr_diffs
from sweepai.utils.chat_logger import ChatLogger
from sweepai.utils.diff import generate_diff
from sweepai.utils.event_logger import posthog
from sweepai.utils.github_utils import ClonedRepo, commit_multi_file_changes, get_github_client, sanitize_string_for_github, validate_and_sanitize_multi_file_changes
from sweepai.utils.progress import TicketProgress
from sweepai.utils.prompt_constructor import HumanMessageCommentPrompt
from sweepai.utils.str_utils import BOT_SUFFIX, FASTER_MODEL_MESSAGE
from sweepai.utils.ticket_utils import fire_and_forget_wrapper, prep_snippets

num_of_snippets_to_query = 30
total_number_of_snippet_tokens = 15_000
num_full_files = 2
num_extended_snippets = 2

ERROR_FORMAT = "❌ {title}\n\nPlease join our [Discourse](https://community.sweep.dev/) to report this issue."


def on_comment(
    repo_full_name: str,
    repo_description: str,
    comment: str,
    pr_path: str | None,
    pr_line_position: int | None,
    username: str,
    installation_id: int,
    pr_number: int = None,
    comment_id: int | None = None,
    chat_logger: Any = None,
    pr: MockPR = None,  # For on_comment calls before PR is created
    repo: Any = None,
    comment_type: str = "comment",
    type: str = "comment",
    tracking_id: str = None,
):
    with logger.contextualize(
        tracking_id=tracking_id,
    ):
        logger.info(
            f"Calling on_comment() with the following arguments: {comment},"
            f" {repo_full_name}, {repo_description}, {pr_path}"
        )
        organization, repo_name = repo_full_name.split("/")
        start_time = time.time()

        _token, g = get_github_client(installation_id)
        repo = g.get_repo(repo_full_name)
        if pr is None:
            pr = repo.get_pull(pr_number)
        pr_title = pr.title
        pr_body = (
            pr.body.split("<details>\n<summary><b>🎉 Latest improvements to Sweep:")[0]
            if pr.body
            and "<details>\n<summary><b>🎉 Latest improvements to Sweep:" in pr.body
            else pr.body
        )
        pr_file_path = None
        diffs = get_pr_diffs(repo, pr)
        pr_chunk = None
        formatted_pr_chunk = None

        assignee = pr.assignee.login if pr.assignee else None
        issue_number_match = re.search(r"Fixes #(?P<issue_number>\d+).", pr_body or "")
        original_issue = None
        if issue_number_match or assignee:
            issue_number = issue_number_match.group("issue_number")
            if not assignee:
                # issue_number = issue_number_match.group("issue_number")
                original_issue = repo.get_issue(int(issue_number))
                author = original_issue.user.login
            else:
                author = assignee
            logger.info(f"Author of original issue is {author}")
            chat_logger = (
                chat_logger
                if chat_logger is not None
                else ChatLogger(
                    {
                        "repo_name": repo_name,
                        "title": "(Comment) " + pr_title,
                        "issue_url": pr.html_url,
                        "pr_file_path": pr_file_path,  # may be None
                        "pr_chunk": pr_chunk,  # may be None
                        "repo_full_name": repo_full_name,
                        "repo_description": repo_description,
                        "comment": comment,
                        "pr_path": pr_path,
                        "pr_line_position": pr_line_position,
                        "username": author,
                        "installation_id": installation_id,
                        "pr_number": pr_number,
                        "type": "comment",
                    },
                    active=True,
                )
                if MONGODB_URI
                else None
            )
        else:
            chat_logger = None

        if chat_logger:
            is_paying_user = chat_logger.is_paying_user()
            use_faster_model = chat_logger.use_faster_model()
        else:
            # Todo: chat_logger is None for MockPRs, which will cause all comments to use GPT-4
            is_paying_user = True
            use_faster_model = False
        
        if use_faster_model:
            raise Exception(FASTER_MODEL_MESSAGE)

        assignee = pr.assignee.login if pr.assignee else None

        metadata = {
            "repo_full_name": repo_full_name,
            "repo_name": repo_name,
            "organization": organization,
            "repo_description": repo_description,
            "installation_id": installation_id,
            "username": username if not username.startswith("sweep") else assignee,
            "function": "on_comment",
            "model": "gpt-4",
            "tier": "pro" if is_paying_user else "free",
            "mode": ENV,
            "pr_path": pr_path,
            "pr_line_position": pr_line_position,
            "pr_number": pr_number or pr.id,
            "pr_html_url": pr.html_url,
            "comment_id": comment_id,
            "comment": comment,
            "issue_number": issue_number if issue_number_match else "",
            "tracking_id": tracking_id,
        }

        logger.bind(**metadata)

        elapsed_time = time.time() - start_time
        posthog.capture(
            username,
            "started",
            properties={
                **metadata,
                "duration": elapsed_time,
                "tracking_id": tracking_id,
            },
        )
        logger.info(f"Getting repo {repo_full_name}")
        file_comment = bool(pr_path) and bool(pr_line_position)

        item_to_react_to = None
        reaction = None

        bot_comment = None

        def edit_comment(new_comment: str) -> None:
            new_comment = sanitize_string_for_github(new_comment)
            if bot_comment is not None:
                bot_comment.edit(new_comment + BOT_SUFFIX)

        try:
            # Check if the PR is closed
            if pr.state == "closed":
                return {"success": True, "message": "PR is closed. No event fired."}
            if comment_id:
                try:
                    item_to_react_to = pr.get_issue_comment(comment_id)
                    reaction = item_to_react_to.create_reaction("eyes")
                except Exception:
                    try:
                        item_to_react_to = pr.get_review_comment(comment_id)
                        reaction = item_to_react_to.create_reaction("eyes")
                    except Exception:
                        pass

                if reaction is not None:
                    # Delete rocket reaction
                    reactions = item_to_react_to.get_reactions()
                    for r in reactions:
                        if (
                            r.content == "rocket"
                            and r.user.login == GITHUB_BOT_USERNAME
                        ):
                            item_to_react_to.delete_reaction(r.id)

            branch_name = (
                pr.head.ref if pr_number else pr.pr_head  # pylint: disable=no-member
            )
            cloned_repo = ClonedRepo(
                repo_full_name,
                installation_id,
                branch=branch_name,
                repo=repo,
                token=_token,
            )

            # Generate diffs for this PR
            pr_diff_string = None
            pr_files_modified = None
            if pr_number:
                patches = []
                pr_files_modified = {}
                files = pr.get_files()
                for file in files:
                    if file.status == "modified":
                        # Get the entire file contents, not just the patch
                        pr_files_modified[file.filename] = repo.get_contents(
                            file.filename, ref=branch_name
                        ).decoded_content.decode("utf-8")

                        patches.append(
                            f'<file file_path="{file.filename}">\n{file.patch}\n</file>'
                        )
                pr_diff_string = (
                    "<files_changed>\n" + "\n".join(patches) + "\n</files_changed>"
                )

            # This means it's a comment on a file
            if file_comment:
                pr_file = repo.get_contents(
                    pr_path, ref=branch_name
                ).decoded_content.decode("utf-8")
                pr_lines = pr_file.splitlines()
                start = max(0, pr_line_position - 11)
                end = min(len(pr_lines), pr_line_position + 10)
                original_line = pr_lines[pr_line_position - 1]
                pr_chunk = "\n".join(pr_lines[start:end])
                pr_file_path = pr_path.strip()
                formatted_pr_chunk = (
                    "\n".join(pr_lines[start : pr_line_position - 1])
                    + f"\n{pr_lines[pr_line_position - 1]} <- COMMENT: {comment.strip()}\n"
                    + "\n".join(pr_lines[pr_line_position:end])
                )
                if comment_id:
                    try:
                        bot_comment = pr.create_review_comment_reply(
                            comment_id, "Working on it..." + BOT_SUFFIX
                        )
                    except Exception as e:
                        print(e)
            else:
                formatted_pr_chunk = None  # pr_file
                bot_comment = pr.create_issue_comment("Working on it..." + BOT_SUFFIX)
            try:
                search_query = (comment).strip("\n")
                formatted_query = (f"{comment}").strip("\n")
                repo_context_manager = prep_snippets(
                    cloned_repo, search_query, TicketProgress(tracking_id="none")
                )
                snippets = repo_context_manager.current_top_snippets
                tree = str(repo_context_manager.dir_obj)
                cloned_repo = repo_context_manager.cloned_repo
            except Exception as e:
                logger.error(traceback.format_exc())
                raise e
            get_documentation_dict(repo)
            docs_results = ""
            logger.info("Getting response from ChatGPT...")
            human_message = HumanMessageCommentPrompt(
                comment=comment,
                repo_name=repo_name,
                repo_description=repo_description if repo_description else "",
                diffs=diffs,
                title=pr_title,
                tree=tree,
                summary=pr_body,
                snippets=snippets,
                pr_file_path=pr_file_path,  # may be None
                pr_chunk=formatted_pr_chunk,  # may be None
                original_line=original_line if pr_chunk else None,
                relevant_docs=docs_results,
            )
            logger.info(f"Human prompt{human_message.construct_prompt()}")

            if use_faster_model:
                raise Exception("GPT-3.5 is not supported for comments")
            sweep_bot = SweepBot.from_system_message_content(
                # human_message=human_message, model="claude-v1.3-100k", repo=repo
                human_message=human_message,
                repo=repo,
                chat_logger=chat_logger,
                model=DEFAULT_GPT4_MODEL,
                cloned_repo=cloned_repo,
            )
        except Exception as e:
            stack_trace = traceback.format_exc()
            logger.error(stack_trace)
            elapsed_time = time.time() - start_time
            posthog.capture(
                username,
                "failed",
                properties={
                    "error": str(e),
                    "reason": f"An error occured! The stack trace is below:\n\n{stack_trace}",
                    "duration": elapsed_time,
                    "tracking_id": tracking_id,
                    **metadata,
                },
            )
            edit_comment(ERROR_FORMAT.format(title=f"An error occured!\n\nThe exception message is:{str(e)}\n\nThe stack trace is:{stack_trace}"))
            raise e

        try:
            logger.info("Fetching files to modify/create...")
            if file_comment:
                formatted_query = f"The user left this comment: <comment>\n{comment}\n</comment>\nThis was where they left their comment:\n<review_code_chunk>\n{formatted_pr_chunk}\n</review_code_chunk>.\n\nResolve their comment."
            file_change_requests, plan = get_files_to_change(
                relevant_snippets=repo_context_manager.current_top_snippets,
                read_only_snippets=repo_context_manager.read_only_snippets,
                problem_statement=formatted_query,
                repo_name=repo_name,
                pr_diffs=pr_diff_string,
                cloned_repo=cloned_repo,
            )
            validate_file_change_requests(file_change_requests, repo_context_manager.cloned_repo)
            file_change_requests = sweep_bot.validate_file_change_requests(
                file_change_requests, branch=branch_name
            )

            sweep_response = "I couldn't find any relevant files to change."
            if file_change_requests:
                table_message = tabulate(
                    [
                        [
                            f"`{file_change_request.filename}`",
                            file_change_request.instructions_display.replace(
                                "\n", "<br/>"
                            ).replace("```", "\\```"),
                        ]
                        for file_change_request in file_change_requests
                    ],
                    headers=["File Path", "Proposed Changes"],
                    tablefmt="pipe",
                )
                sweep_response = (
                    f"I am making the following changes:\n\n{table_message}"
                )
            quoted_comment = "> " + comment.replace("\n", "\n> ")
            response_for_user = (
                f"{quoted_comment}\n\nHi @{username},\n\n{sweep_response}"
            )
            if pr_number:
                edit_comment(response_for_user)

            sweep_bot.comment_pr_diff_str = pr_diff_string
            sweep_bot.comment_pr_files_modified = pr_files_modified
            modify_files_dict, changes_made, file_change_requests = handle_file_change_requests(
                file_change_requests=file_change_requests,
                request=file_comment,
                branch_name=branch_name,
                sweep_bot=sweep_bot,
                username=username,
                installation_id=installation_id,
                chat_logger=chat_logger,
            )
            logger.info("\n".join(generate_diff(file_data["original_contents"], file_data["contents"]) for file_data in modify_files_dict.values()))
            commit_message = f"feat: Updated {len(modify_files_dict or [])} files"[:50]
            try:
                new_file_contents_to_commit = {file_path: file_data["contents"] for file_path, file_data in modify_files_dict.items()}
                previous_file_contents_to_commit = copy.deepcopy(new_file_contents_to_commit)
                new_file_contents_to_commit, files_removed = validate_and_sanitize_multi_file_changes(sweep_bot.repo, new_file_contents_to_commit, file_change_requests)
                if files_removed and username:
                    posthog.capture(
                        username,
                        "polluted_commits_error",
                        properties={
                            "old_keys": ",".join(previous_file_contents_to_commit.keys()),
                            "new_keys": ",".join(new_file_contents_to_commit.keys()) 
                        },
                    )
                commit_multi_file_changes(sweep_bot.repo, new_file_contents_to_commit, commit_message, branch_name)
            except Exception as e:
                logger.info(f"Error in updating file{e}")
                raise e
            try:
                if comment_id:
                    if changes_made:
                        response_for_user = "Done."
                    else:
                        response_for_user = 'I wasn\'t able to make changes. This could be due to an unclear request or a bug in my code.\n As a reminder, comments on a file only modify that file. Comments on a PR (at the bottom of the "conversation" tab) can modify the entire PR. Please try again or contact us on [Discourse](https://community.sweep.dev/)'
            except Exception as e:
                logger.error(f"Failed to reply to comment: {e}")

            if not isinstance(pr, MockPR):
                if pr.user.login == GITHUB_BOT_USERNAME and pr.title.startswith(
                    "[DRAFT] "
                ):
                    # Update the PR title to remove the "[DRAFT]" prefix
                    pr.edit(title=pr.title.replace("[DRAFT] ", "", 1))
            logger.info("Done!")
        except NoFilesException:
            elapsed_time = time.time() - start_time
            posthog.capture(
                username,
                "failed",
                properties={
                    "error": "No files to change",
                    "reason": "No files to change",
                    "duration": elapsed_time,
                    **metadata,
                },
            )
            edit_comment(ERROR_FORMAT.format(title="Could not find files to change"))
            return {"success": True, "message": "No files to change."}
        except Exception as e:
            stack_trace = traceback.format_exc()
            logger.error(stack_trace)
            elapsed_time = time.time() - start_time
            posthog.capture(
                username,
                "failed",
                properties={
                    "error": str(e),
                    "reason": "Failed to make changes",
                    "duration": elapsed_time,
                    **metadata,
                },
            )
            edit_comment(ERROR_FORMAT.format(title=f"Failed to make changes:\n\nThe exception message is:{str(e)}\n\nThe stack trace is:{stack_trace}"))
            raise e

        # Delete eyes
        if reaction is not None:
            item_to_react_to.delete_reaction(reaction.id)

        try:
            item_to_react_to = pr.get_issue_comment(comment_id)
            reaction = item_to_react_to.create_reaction("rocket")
        except Exception:
            try:
                item_to_react_to = pr.get_review_comment(comment_id)
                reaction = item_to_react_to.create_reaction("rocket")
            except Exception:
                pass

        if response_for_user is not None:
            try:
                edit_comment(f"## 🚀 Wrote Changes\n\n{response_for_user}")
            except Exception:
                pass

        elapsed_time = time.time() - start_time
        # make async
        fire_and_forget_wrapper(posthog.capture)(
            username,
            "success",
            properties={
                **metadata,
                "tracking_id": tracking_id,
                "duration": elapsed_time,
            },
        )
        return {"success": True}
