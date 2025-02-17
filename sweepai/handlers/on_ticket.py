"""
on_ticket is the main function that is called when a new issue is created.
It is only called by the webhook handler in sweepai/api.py.
"""

import copy
import os
import traceback
from time import time

import openai
import yaml
import yamllint.config as yamllint_config
from github import BadCredentialsException
from github.WorkflowRun import WorkflowRun
from github.PullRequest import PullRequest as GithubPullRequest
from loguru import logger
from tabulate import tabulate
from yamllint import linter


from sweepai.core.context_pruning import RepoContextManager
from sweepai.core.sweep_bot import GHA_PROMPT
from sweepai.agents.image_description_bot import ImageDescriptionBot
from sweepai.config.client import (
    RESET_FILE,
    REVERT_CHANGED_FILES_TITLE,
    SweepConfig,
    get_documentation_dict,
    get_gha_enabled,
)
from sweepai.config.server import (
    DEPLOYMENT_GHA_ENABLED,
    ENV,
    GITHUB_LABEL_NAME,
    IS_SELF_HOSTED,
    MONGODB_URI,
)
from sweepai.core.entities import (
    FileChangeRequest,
    MaxTokensExceeded,
    MockPR,
    NoFilesException,
    PullRequest,
)
from sweepai.core.pr_reader import PRReader
from sweepai.core.sweep_bot import SweepBot, get_files_to_change, get_files_to_change_for_gha, validate_file_change_requests
from sweepai.handlers.create_pr import (
    create_config_pr,
    handle_file_change_requests,
)
from sweepai.utils.image_utils import get_image_contents_from_urls, get_image_urls_from_issue
from sweepai.utils.issue_validator import validate_issue
from sweepai.utils.ticket_rendering_utils import add_emoji, process_summary, remove_emoji, create_error_logs, get_payment_messages, get_comment_header, send_email_to_user, get_failing_gha_logs, rewrite_pr_description, raise_on_no_file_change_requests, get_branch_diff_text, construct_sweep_bot, handle_empty_repository, delete_old_prs, custom_config
from sweepai.utils.validate_license import validate_license
from sweepai.utils.buttons import Button, ButtonList
from sweepai.utils.chat_logger import ChatLogger
from sweepai.utils.event_logger import posthog
from sweepai.utils.github_utils import (
    CURRENT_USERNAME,
    ClonedRepo,
    commit_multi_file_changes,
    convert_pr_draft_field,
    get_github_client,
    sanitize_string_for_github,
    validate_and_sanitize_multi_file_changes,
)
from sweepai.utils.slack_utils import add_slack_context
from sweepai.utils.str_utils import (
    BOT_SUFFIX,
    FASTER_MODEL_MESSAGE,
    blockquote,
    bold,
    bot_suffix,
    checkbox_template,
    collapsible_template,
    create_checkbox,
    create_collapsible,
    discord_suffix,
    get_hash,
    sep,
    strip_sweep,
    to_branch_name,
)
from sweepai.utils.ticket_utils import (
    center,
    fetch_relevant_files,
    fire_and_forget_wrapper,
    prep_snippets,
)
from sweepai.utils.user_settings import UserSettings

def on_ticket(
    title: str,
    summary: str,
    issue_number: int,
    issue_url: str, # purely for logging purposes
    username: str,
    repo_full_name: str,
    repo_description: str,
    installation_id: int,
    comment_id: int = None,
    edited: bool = False,
    tracking_id: str | None = None,
):
    if not os.environ.get("CLI"):
        assert validate_license(), "License key is invalid or expired. Please contact us at team@sweep.dev to upgrade to an enterprise license."
    with logger.contextualize(
        tracking_id=tracking_id,
    ):
        if tracking_id is None:
            tracking_id = get_hash()
        on_ticket_start_time = time()
        logger.info(f"Starting on_ticket with title {title} and summary {summary}")
        (
            title,
            slow_mode,
            do_map,
            subissues_mode,
            sandbox_mode,
            fast_mode,
            lint_mode,
        ) = strip_sweep(title)
        summary, repo_name, user_token, g, repo, current_issue, assignee, overrided_branch_name = process_summary(summary, issue_number, repo_full_name, installation_id)

        chat_logger: ChatLogger = (
            ChatLogger(
                {
                    "repo_name": repo_name,
                    "title": title,
                    "summary": summary,
                    "issue_number": issue_number,
                    "issue_url": issue_url,
                    "username": (
                        username if not username.startswith("sweep") else assignee
                    ),
                    "repo_full_name": repo_full_name,
                    "repo_description": repo_description,
                    "installation_id": installation_id,
                    "type": "ticket",
                    "mode": ENV,
                    "comment_id": comment_id,
                    "edited": edited,
                    "tracking_id": tracking_id,
                },
                active=True,
            )
            if MONGODB_URI
            else None
        )

        if chat_logger and not IS_SELF_HOSTED:
            is_paying_user = chat_logger.is_paying_user()
            use_faster_model = chat_logger.use_faster_model()
        else:
            is_paying_user = True
            use_faster_model = False

        if use_faster_model:
            raise Exception(FASTER_MODEL_MESSAGE)

        if fast_mode:
            use_faster_model = True

        if not comment_id and not edited and chat_logger and not sandbox_mode:
            fire_and_forget_wrapper(chat_logger.add_successful_ticket)(
                gpt3=use_faster_model
            )

        organization, repo_name = repo_full_name.split("/")
        metadata = {
            "issue_url": issue_url,
            "repo_full_name": repo_full_name,
            "organization": organization,
            "repo_name": repo_name,
            "repo_description": repo_description,
            "username": username,
            "comment_id": comment_id,
            "title": title,
            "installation_id": installation_id,
            "function": "on_ticket",
            "edited": edited,
            "model": "gpt-3.5" if use_faster_model else "gpt-4",
            "tier": "pro" if is_paying_user else "free",
            "mode": ENV,
            "slow_mode": slow_mode,
            "do_map": do_map,
            "subissues_mode": subissues_mode,
            "sandbox_mode": sandbox_mode,
            "fast_mode": fast_mode,
            "is_self_hosted": IS_SELF_HOSTED,
            "tracking_id": tracking_id,
        }
        fire_and_forget_wrapper(posthog.capture)(
            username, "started", properties=metadata
        )

        try:
            if current_issue.state == "closed":
                fire_and_forget_wrapper(posthog.capture)(
                    username,
                    "issue_closed",
                    properties={
                        **metadata,
                        "duration": round(time() - on_ticket_start_time),
                    },
                )
                return {"success": False, "reason": "Issue is closed"}

            fire_and_forget_wrapper(add_emoji)(current_issue, comment_id)
            fire_and_forget_wrapper(remove_emoji)(
                current_issue, comment_id, content_to_delete="rocket"
            )
            fire_and_forget_wrapper(remove_emoji)(
                current_issue, comment_id, content_to_delete="confused"
            )
            fire_and_forget_wrapper(current_issue.edit)(body=summary)

            replies_text = ""
            summary = summary if summary else ""

            fire_and_forget_wrapper(delete_old_prs)(repo, issue_number)

            progress_headers = [
                None,
                "Step 1: 🔎 Searching",
                "Step 2: ⌨️ Coding",
                "Step 3: 🔁 Code Review",
            ]

            issue_comment = None
            payment_message, payment_message_start = get_payment_messages(
                chat_logger
            )

            config_pr_url = None
            user_settings: UserSettings = UserSettings.from_username(username=username)
            user_settings_message = user_settings.get_message()

            cloned_repo: ClonedRepo = ClonedRepo(
                repo_full_name,
                installation_id=installation_id,
                token=user_token,
                repo=repo,
                branch=overrided_branch_name,
            )
            # check that repo's directory is non-empty
            if os.listdir(cloned_repo.cached_dir) == []:
                handle_empty_repository(comment_id, current_issue, progress_headers, issue_comment)
                return {"success": False}
            indexing_message = (
                "I'm searching for relevant snippets in your repository. If this is your first"
                " time using Sweep, I'm indexing your repository. You can monitor the progress using the progress dashboard"
            )
            first_comment = (
                f"{get_comment_header(0, g, repo_full_name, user_settings, progress_headers, tracking_id, payment_message_start, user_settings_message)}\n{sep}I am currently looking into this ticket! I"
                " will update the progress of the ticket in this comment. I am currently"
                f" searching through your code, looking for relevant snippets.\n{sep}##"
                f" {progress_headers[1]}\n{indexing_message}{bot_suffix}{discord_suffix}"
            )
            # Find Sweep's previous comment
            comments = []
            for comment in current_issue.get_comments():
                comments.append(comment)
                if comment.user.login == CURRENT_USERNAME:
                    issue_comment = comment
                    break
            if issue_comment is None:
                issue_comment = current_issue.create_comment(first_comment)
            else:
                fire_and_forget_wrapper(issue_comment.edit)(first_comment)
            old_edit = issue_comment.edit
            issue_comment.edit = lambda msg: old_edit(msg + BOT_SUFFIX)
            past_messages = {}
            current_index = 0
            table = None
            initial_sandbox_response = -1
            initial_sandbox_response_file = None

            def refresh_token():
                user_token, g = get_github_client(installation_id)
                repo = g.get_repo(repo_full_name)
                return user_token, g, repo

            def edit_sweep_comment(
                message: str,
                index: int,
                pr_message="",
                done=False,
                add_bonus_message=True,
            ):
                nonlocal current_index, user_token, g, repo, issue_comment, initial_sandbox_response, initial_sandbox_response_file
                message = sanitize_string_for_github(message)
                if pr_message:
                    pr_message = sanitize_string_for_github(pr_message)
                # -1 = error, -2 = retry
                # Only update the progress bar if the issue generation errors.
                errored = index == -1
                if index >= 0:
                    past_messages[index] = message
                    current_index = index

                agg_message = None
                # Include progress history
                # index = -2 is reserved for
                for i in range(
                    current_index + 2
                ):  # go to next header (for Working on it... text)
                    if i == 0 or i >= len(progress_headers):
                        continue  # skip None header
                    header = progress_headers[i]
                    if header is not None:
                        header = "## " + header + "\n"
                    else:
                        header = "No header\n"
                    msg = header + (past_messages.get(i) or "Working on it...")
                    if agg_message is None:
                        agg_message = msg
                    else:
                        agg_message = agg_message + f"\n{sep}" + msg

                suffix = bot_suffix + discord_suffix
                if errored:
                    agg_message = (
                        "## ❌ Unable to Complete PR"
                        + "\n"
                        + message
                        + (
                            "\n\nFor bonus GPT-4 tickets, please report this bug on"
                            f" **[Discourse](https://community.sweep.dev/)** (tracking ID: `{tracking_id}`)."
                            if add_bonus_message
                            else ""
                        )
                    )
                    if table is not None:
                        agg_message = (
                            agg_message
                            + f"\n{sep}Please look at the generated plan. If something looks"
                            f" wrong, please add more details to your issue.\n\n{table}"
                        )
                    suffix = bot_suffix  # don't include discord suffix for error messages

                # Update the issue comment
                msg = f"{get_comment_header(current_index, g, repo_full_name, user_settings, progress_headers, tracking_id, payment_message_start, user_settings_message, errored=errored, pr_message=pr_message, done=done, initial_sandbox_response=initial_sandbox_response, initial_sandbox_response_file=initial_sandbox_response_file, config_pr_url=config_pr_url)}\n{sep}{agg_message}{suffix}"
                try:
                    issue_comment.edit(msg)
                except BadCredentialsException:
                    logger.error(
                        f"Bad credentials, refreshing token (tracking ID: `{tracking_id}`)"
                    )
                    user_token, g = get_github_client(installation_id)
                    repo = g.get_repo(repo_full_name)

                    issue_comment = None
                    for comment in comments:
                        if comment.user.login == CURRENT_USERNAME:
                            issue_comment = comment
                    current_issue = repo.get_issue(number=issue_number)
                    if issue_comment is None:
                        issue_comment = current_issue.create_comment(msg)
                    else:
                        issue_comment = [
                            comment
                            for comment in current_issue.get_comments()
                            if comment.user.login == CURRENT_USERNAME
                        ][0]
                        issue_comment.edit(msg)

            if use_faster_model:
                edit_sweep_comment(
                    FASTER_MODEL_MESSAGE, -1, add_bonus_message=False
                )
                posthog.capture(
                    username,
                    "ran_out_of_tickets",
                    properties={
                        **metadata,
                        "duration": round(time() - on_ticket_start_time),
                    },
                )
                fire_and_forget_wrapper(add_emoji)(
                    current_issue, comment_id, reaction_content="confused"
                )
                fire_and_forget_wrapper(remove_emoji)(content_to_delete="eyes")
                return {
                    "success": False,
                    "error_message": "We deprecated supporting GPT 3.5.",
                }
            
            internal_message_summary = summary
            internal_message_summary += add_slack_context(internal_message_summary)
            error_message = validate_issue(title + internal_message_summary)
            if error_message:
                logger.warning(f"Validation error: {error_message}")
                edit_sweep_comment(
                    (
                        f"The issue was rejected with the following response:\n\n{bold(error_message)}"
                    ),
                    -1,
                )
                fire_and_forget_wrapper(add_emoji)(
                    current_issue, comment_id, reaction_content="confused"
                )
                fire_and_forget_wrapper(remove_emoji)(content_to_delete="eyes")
                posthog.capture(
                    username,
                    "invalid_issue",
                    properties={
                        **metadata,
                        "duration": round(time() - on_ticket_start_time),
                    },
                )
                return {"success": True}

            prs_extracted = PRReader.extract_prs(repo, summary)
            if prs_extracted:
                internal_message_summary += "\n\n" + prs_extracted
                edit_sweep_comment(
                    create_collapsible(
                        "I found that you mentioned the following Pull Requests that might be important:",
                        blockquote(
                            prs_extracted,
                        ),
                    ),
                    1,
                )

            try:
                # search/context manager
                logger.info("Searching for relevant snippets...")
                # fetch images from body of issue
                image_urls = get_image_urls_from_issue(issue_number, repo_full_name, installation_id)
                image_contents = get_image_contents_from_urls(image_urls)
                if image_contents: # doing it here to avoid editing the original issue
                    internal_message_summary += ImageDescriptionBot().describe_images(text=title + internal_message_summary, images=image_contents)
                
                snippets, tree, _, repo_context_manager = fetch_relevant_files(
                    cloned_repo,
                    title,
                    internal_message_summary,
                    replies_text,
                    username,
                    metadata,
                    on_ticket_start_time,
                    tracking_id,
                    is_paying_user,
                    issue_url,
                    chat_logger,
                    images=image_contents
                )
                cloned_repo = repo_context_manager.cloned_repo
            except Exception as e:
                edit_sweep_comment(
                    (
                        "It looks like an issue has occurred around fetching the files."
                        f" The exception was {str(e)}. If this error persists"
                        f" contact team@sweep.dev.\n\n> @{username}, editing this issue description to include more details will automatically make me relaunch. Please join our Discourse (https://community.sweep.dev/) for support (tracking_id={tracking_id})"
                    ),
                    -1,
                )
                raise Exception("Failed to fetch files") from e
            _user_token, g = get_github_client(installation_id)
            user_token, g, repo = refresh_token()
            cloned_repo.token = user_token
            repo = g.get_repo(repo_full_name)

            # Fetch git commit history
            if not repo_description:
                repo_description = "No description provided."

            internal_message_summary += replies_text

            get_documentation_dict(repo)
            docs_results = ""
            sweep_bot = construct_sweep_bot(
                repo=repo,
                repo_name=repo_name,
                issue_url=issue_url,
                repo_description=repo_description,
                title=title,
                message_summary=internal_message_summary,
                cloned_repo=cloned_repo,
                chat_logger=chat_logger,
                snippets=snippets,
                tree=tree,
                comments=comments,
            )
            # Check repository for sweep.yml file.
            sweep_yml_exists = False
            sweep_yml_failed = False
            for content_file in repo.get_contents(""):
                if content_file.name == "sweep.yaml":
                    sweep_yml_exists = True

                    # Check if YAML is valid
                    yaml_content = content_file.decoded_content.decode("utf-8")
                    sweep_yaml_dict = {}
                    try:
                        sweep_yaml_dict = yaml.safe_load(yaml_content)
                    except Exception:
                        logger.error(f"Failed to load YAML file: {yaml_content}")
                    if len(sweep_yaml_dict) > 0:
                        break
                    linter_config = yamllint_config.YamlLintConfig(custom_config)
                    problems = list(linter.run(yaml_content, linter_config))
                    if problems:
                        errors = [
                            f"Line {problem.line}: {problem.desc} (rule: {problem.rule})"
                            for problem in problems
                        ]
                        error_message = "\n".join(errors)
                        markdown_error_message = f"**There is something wrong with your [sweep.yaml](https://github.com/{repo_full_name}/blob/main/sweep.yaml):**\n```\n{error_message}\n```"
                        sweep_yml_failed = True
                        logger.error(markdown_error_message)
                        edit_sweep_comment(markdown_error_message, -1)
                    else:
                        logger.info("The YAML file is valid. No errors found.")
                    break

            # If sweep.yaml does not exist, then create a new PR that simply creates the sweep.yaml file.
            if not sweep_yml_exists:
                try:
                    logger.info("Creating sweep.yaml file...")
                    config_pr = create_config_pr(sweep_bot, cloned_repo=cloned_repo)
                    config_pr_url = config_pr.html_url
                    edit_sweep_comment(message="", index=-2)
                except Exception as e:
                    logger.error(
                        "Failed to create new branch for sweep.yaml file.\n",
                        e,
                        traceback.format_exc(),
                    )
            else:
                logger.info("sweep.yaml file already exists.")

            try:
                # ANALYZE SNIPPETS
                newline = "\n"
                edit_sweep_comment(
                    "I found the following snippets in your repository. I will now analyze"
                    " these snippets and come up with a plan."
                    + "\n\n"
                    + create_collapsible(
                        "Some code snippets I think are relevant in decreasing order of relevance (click to expand). If some file is missing from here, you can mention the path in the ticket description.",
                        "\n".join(
                            [
                                f"https://github.com/{organization}/{repo_name}/blob/{repo.get_commits()[0].sha}/{snippet.file_path}#L{max(snippet.start, 1)}-L{min(snippet.end, snippet.content.count(newline) - 1)}\n"
                                for snippet in snippets
                            ]
                        ),
                    )
                    + (
                        create_collapsible(
                            "I also found that you mentioned the following Pull Requests that may be helpful:",
                            blockquote(prs_extracted),
                        )
                        if prs_extracted
                        else ""
                    )
                    + (f"\n\n{docs_results}\n\n" if docs_results else ""),
                    1,
                )
                logger.info("Fetching files to modify/create...")
                file_change_requests, plan = get_files_to_change(
                    relevant_snippets=repo_context_manager.current_top_snippets,
                    read_only_snippets=repo_context_manager.read_only_snippets,
                    problem_statement=f"{title}\n\n{internal_message_summary}",
                    repo_name=repo_full_name,
                    cloned_repo=cloned_repo,
                    images=image_contents
                )
                validate_file_change_requests(file_change_requests, cloned_repo)
                raise_on_no_file_change_requests(title, summary, edit_sweep_comment, file_change_requests)

                file_change_requests: list[
                    FileChangeRequest
                ] = sweep_bot.validate_file_change_requests(
                    file_change_requests,
                )

                table = tabulate(
                    [
                        [
                            file_change_request.entity_display,
                            file_change_request.instructions_display.replace(
                                "\n", "<br/>"
                            ).replace("```", "\\```"),
                        ]
                        for file_change_request in file_change_requests
                        if file_change_request.change_type != "check"
                    ],
                    headers=["File Path", "Proposed Changes"],
                    tablefmt="pipe",
                )

                files_progress: list[tuple[str, str, str, str]] = [
                    (
                        file_change_request.entity_display,
                        file_change_request.instructions_display,
                        "⏳ In Progress",
                        "",
                    )
                    for file_change_request in file_change_requests
                ]

                checkboxes_progress: list[tuple[str, str, str]] = [
                    (
                        file_change_request.entity_display,
                        file_change_request.instructions_display,
                        " ",
                    )
                    for file_change_request in file_change_requests
                    if not file_change_request.change_type == "check"
                ]
                checkboxes_contents = "\n".join(
                    [
                        create_checkbox(
                            f"`{filename}`", blockquote(instructions), check == "X"
                        )
                        for filename, instructions, check in checkboxes_progress
                    ]
                )
                create_collapsible("Checklist", checkboxes_contents, opened=True)

                file_change_requests[0].status = "running"

                condensed_checkboxes_contents = "\n".join(
                    [
                        create_checkbox(f"`{filename}`", "", check == "X").strip()
                        for filename, instructions, check in checkboxes_progress
                    ]
                )
                condensed_checkboxes_collapsible = create_collapsible(
                    "Checklist", condensed_checkboxes_contents, opened=True
                )

                current_issue = repo.get_issue(number=issue_number)
                current_issue.edit(
                    body=summary + "\n\n" + condensed_checkboxes_collapsible
                )

                delete_branch = False
                pull_request: PullRequest = PullRequest(
                    title="Sweep: " + title,
                    branch_name="sweep/" + to_branch_name(title),
                    content="",
                )
                logger.info("Making PR...")
                pull_request.branch_name = sweep_bot.create_branch(
                    pull_request.branch_name, base_branch=overrided_branch_name
                )
                modify_files_dict, changed_file, file_change_requests = handle_file_change_requests(
                    file_change_requests=file_change_requests,
                    request=sweep_bot.human_message.get_issue_request(),
                    branch_name=pull_request.branch_name,
                    sweep_bot=sweep_bot,
                    username=username,
                    installation_id=installation_id,
                    chat_logger=chat_logger,
                )
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
                    commit = commit_multi_file_changes(sweep_bot.repo, new_file_contents_to_commit, commit_message, pull_request.branch_name)
                except Exception as e:
                    logger.info(f"Error in updating file{e}")
                    raise e
                # set all fcrs without a corresponding change to be failed
                for file_change_request in file_change_requests:
                    if file_change_request.status != "succeeded":
                        file_change_request.status = "failed"
                    # also update all commit hashes associated with the fcr
                    file_change_request.commit_hash_url = commit.html_url if commit else None
                edit_sweep_comment(checkboxes_contents, 2)
                if not file_change_requests:
                    raise NoFilesException()
                changed_files = []

                # append all files that have been changed
                if modify_files_dict:
                    for file_name, _ in modify_files_dict.items():
                        changed_files.append(file_name)
                commit_hash: str = (
                    commit
                    if isinstance(commit, str)
                    else (
                        commit.sha
                        if commit is not None
                        else repo.get_branch(
                            pull_request.branch_name
                        ).commit.sha
                    )
                )
                commit_url = (
                    f"https://github.com/{repo_full_name}/commit/{commit_hash}"
                )
                commit_url_display = (
                    f"<a href='{commit_url}'><code>{commit_hash[:7]}</code></a>"
                )
                create_error_logs(
                    commit_url_display,
                    None,
                    status=(
                        "✓"
                    ),
                )
                checkboxes_progress = [
                    (
                        file_change_request.display_summary
                        + " "
                        + file_change_request.status_display
                        + " "
                        + (file_change_request.commit_hash_url or "")
                        + f" [Edit]({file_change_request.get_edit_url(repo.full_name, pull_request.branch_name)})",
                        file_change_request.instructions_ticket_display
                        + f"\n\n{file_change_request.diff_display}",
                        (
                            "X"
                            if file_change_request.status
                            in ("succeeded", "failed")
                            else " "
                        ),
                    )
                    for file_change_request in file_change_requests
                ]
                checkboxes_contents = "\n".join(
                    [
                        checkbox_template.format(
                            check=check,
                            filename=filename,
                            instructions=blockquote(instructions),
                        )
                        for filename, instructions, check in checkboxes_progress
                    ]
                )
                collapsible_template.format(
                    summary="Checklist",
                    body=checkboxes_contents,
                    opened="open",
                )
                condensed_checkboxes_contents = "\n".join(
                    [
                        checkbox_template.format(
                            check=check,
                            filename=filename,
                            instructions="",
                        ).strip()
                        for filename, instructions, check in checkboxes_progress
                        if not instructions.lower().startswith("run")
                    ]
                )
                condensed_checkboxes_collapsible = collapsible_template.format(
                    summary="Checklist",
                    body=condensed_checkboxes_contents,
                    opened="open",
                )

                try:
                    current_issue = repo.get_issue(number=issue_number)
                except BadCredentialsException:
                    user_token, g, repo = refresh_token()
                    cloned_repo.token = user_token

                current_issue.edit(
                    body=summary + "\n\n" + condensed_checkboxes_collapsible
                )

                logger.info(files_progress)
                edit_sweep_comment(checkboxes_contents, 2)

                checkboxes_contents = "\n".join(
                    [
                        checkbox_template.format(
                            check=check,
                            filename=filename,
                            instructions=blockquote(instructions),
                        )
                        for filename, instructions, check in checkboxes_progress
                    ]
                )
                condensed_checkboxes_contents = "\n".join(
                    [
                        checkbox_template.format(
                            check=check,
                            filename=filename,
                            instructions="",
                        ).strip()
                        for filename, instructions, check in checkboxes_progress
                        if not instructions.lower().startswith("run")
                    ]
                )
                condensed_checkboxes_collapsible = collapsible_template.format(
                    summary="Checklist",
                    body=condensed_checkboxes_contents,
                    opened="open",
                )
                for _ in range(3):
                    try:
                        current_issue.edit(
                            body=summary + "\n\n" + condensed_checkboxes_collapsible
                        )
                        break
                    except Exception:
                        from time import sleep
                        sleep(1)
                edit_sweep_comment(checkboxes_contents, 2)
                pr_changes = MockPR(
                    file_count=len(modify_files_dict),
                    title=pull_request.title,
                    body="", # overrided later
                    pr_head=pull_request.branch_name,
                    base=sweep_bot.repo.get_branch(
                        SweepConfig.get_branch(sweep_bot.repo)
                    ).commit,
                    head=sweep_bot.repo.get_branch(pull_request.branch_name).commit,
                )
                pr_changes = rewrite_pr_description(issue_number, repo, overrided_branch_name, pull_request, pr_changes)

                edit_sweep_comment(
                    "I have finished coding the issue. I am now reviewing it for completeness.",
                    3,
                )
                change_location = f" [`{pr_changes.pr_head}`](https://github.com/{repo_full_name}/commits/{pr_changes.pr_head}).\n\n"
                review_message = (
                    "Here are my self-reviews of my changes at" + change_location
                )

                try:
                    fire_and_forget_wrapper(remove_emoji)(content_to_delete="eyes")
                except Exception:
                    pass

                changes_required, review_message = False, ""
                if changes_required:
                    edit_sweep_comment(
                        review_message
                        + "\n\nI finished incorporating these changes.",
                        3,
                    )
                else:
                    edit_sweep_comment(
                        f"I have finished reviewing the code for completeness. I did not find errors for {change_location}",
                        3,
                    )

                revert_buttons = []
                for changed_file in set(changed_files):
                    revert_buttons.append(
                        Button(label=f"{RESET_FILE} {changed_file}")
                    )
                revert_buttons_list = ButtonList(
                    buttons=revert_buttons, title=REVERT_CHANGED_FILES_TITLE
                )

                # delete failing sweep yaml if applicable
                if sweep_yml_failed:
                    try:
                        repo.delete_file(
                            "sweep.yaml",
                            "Delete failing sweep.yaml",
                            branch=pr_changes.pr_head,
                            sha=repo.get_contents("sweep.yaml").sha,
                        )
                    except Exception:
                        pass

                # create draft pr, then convert to regular pr later
                pr: GithubPullRequest = repo.create_pull(
                    title=pr_changes.title,
                    body=pr_changes.body,
                    head=pr_changes.pr_head,
                    base=overrided_branch_name or SweepConfig.get_branch(repo),
                    # removed draft PR
                    draft=False,
                )

                try:
                    pr.add_to_assignees(username)
                except Exception as e:
                    logger.error(
                        f"Failed to add assignee {username}: {e}, probably a bot."
                    )

                if revert_buttons:
                    pr.create_issue_comment(
                        revert_buttons_list.serialize() + BOT_SUFFIX
                    )

                # add comments before labelling
                pr.add_to_labels(GITHUB_LABEL_NAME)
                current_issue.create_reaction("rocket")
                heres_pr_message = f'<h1 align="center">🚀 Here\'s the PR! <a href="{pr.html_url}">#{pr.number}</a></h1>'
                progress_message = ''
                edit_sweep_comment(
                    review_message + "\n\nSuccess! 🚀",
                    4,
                    pr_message=(
                        f"{center(heres_pr_message)}\n{center(progress_message)}\n{center(payment_message_start)}"
                    ),
                    done=True,
                )

                send_email_to_user(title, issue_number, username, repo_full_name, tracking_id, repo_name, g, file_change_requests, pr_changes, pr)

                # poll for github to check when gha are done
                total_poll_attempts = 0
                total_edit_attempts = 0
                SLEEP_DURATION_SECONDS = 15
                GITHUB_ACTIONS_ENABLED = get_gha_enabled(repo=repo) and DEPLOYMENT_GHA_ENABLED
                GHA_MAX_EDIT_ATTEMPTS = 5 # max number of times to edit PR
                current_commit = pr.head.sha
                while True and GITHUB_ACTIONS_ENABLED:
                    logger.info(
                        f"Polling to see if Github Actions have finished... {total_poll_attempts}"
                    )
                    # we wait at most 60 minutes
                    if total_poll_attempts * SLEEP_DURATION_SECONDS // 60 >= 60:
                        break
                    else:
                        # wait one minute between check attempts
                        total_poll_attempts += 1
                        from time import sleep

                        sleep(SLEEP_DURATION_SECONDS)
                    # refresh the pr
                    pr = repo.get_pull(pr.number)
                    current_commit = repo.get_pull(pr.number).head.sha # IMPORTANT: resync PR otherwise you'll fetch old GHA runs
                    runs: list[WorkflowRun] = list(repo.get_workflow_runs(branch=pr.head.ref, head_sha=current_commit))
                    # if all runs have succeeded or have no result, break
                    if all([run.conclusion in ["success", None] for run in runs]):
                        break
                    # if any of them have failed we retry
                    if any([run.conclusion == "failure" for run in runs]):
                        failed_runs = [
                            run for run in runs if run.conclusion == "failure"
                        ]

                        failed_gha_logs: list[str] = get_failing_gha_logs(
                            failed_runs,
                            installation_id,
                        )
                        if failed_gha_logs:
                            # make edits to the PR
                            # TODO: look into rollbacks so we don't continue adding onto errors
                            cloned_repo = ClonedRepo( # reinitialize cloned_repo to avoid conflicts
                                repo_full_name,
                                installation_id=installation_id,
                                token=user_token,
                                repo=repo,
                                branch=pr.head.ref,
                            )
                            diffs = get_branch_diff_text(repo=repo, branch=pr.head.ref, base_branch=pr.base.ref)
                            problem_statement = f"{title}\n{internal_message_summary}\n{replies_text}"
                            all_information_prompt = GHA_PROMPT.format(
                                problem_statement=problem_statement,
                                github_actions_logs=failed_gha_logs,
                                changes_made=diffs,
                            )
                            repo_context_manager: RepoContextManager = prep_snippets(cloned_repo=cloned_repo, query=(title + internal_message_summary + replies_text).strip("\n"), ticket_progress=None) # need to do this, can use the old query for speed
                            sweep_bot: SweepBot = construct_sweep_bot(
                                repo=repo,
                                repo_name=repo_name,
                                issue_url=issue_url,
                                repo_description=repo_description,
                                title="Fix the following errors to complete the user request.",
                                message_summary=all_information_prompt,
                                cloned_repo=cloned_repo,
                                chat_logger=chat_logger,
                                snippets=snippets,
                                tree=tree,
                                comments=comments,
                            )
                            file_change_requests, plan = get_files_to_change_for_gha(
                                relevant_snippets=repo_context_manager.current_top_snippets,
                                read_only_snippets=repo_context_manager.read_only_snippets,
                                problem_statement=all_information_prompt,
                                updated_files=modify_files_dict,
                                cloned_repo=cloned_repo,
                                chat_logger=chat_logger,
                            )
                            validate_file_change_requests(file_change_requests, cloned_repo)
                            previous_modify_files_dict: dict[str, dict[str, str | list[str]]] | None = None
                            modify_files_dict, _, file_change_requests = handle_file_change_requests(
                                file_change_requests=file_change_requests,
                                request=sweep_bot.human_message.get_issue_request(),
                                branch_name=pull_request.branch_name,
                                sweep_bot=sweep_bot,
                                username=username,
                                installation_id=installation_id,
                                chat_logger=chat_logger,
                                previous_modify_files_dict=previous_modify_files_dict,
                            )
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
                                commit = commit_multi_file_changes(sweep_bot.repo, new_file_contents_to_commit, commit_message, pull_request.branch_name)
                            except Exception as e:
                                logger.info(f"Error in updating file{e}")
                                raise e
                            total_edit_attempts += 1
                            if total_edit_attempts >= GHA_MAX_EDIT_ATTEMPTS:
                                logger.info(f"Tried to edit PR {GHA_MAX_EDIT_ATTEMPTS} times, giving up.")
                                break
                    # if none of the runs have completed we wait and poll github
                    logger.info(
                        f"No Github Actions have failed yet and not all have succeeded yet, waiting for {SLEEP_DURATION_SECONDS} seconds before polling again..."
                    )
                # break from main for loop
                convert_pr_draft_field(pr, is_draft=False, installation_id=installation_id)
            except MaxTokensExceeded as e:
                logger.info("Max tokens exceeded")
                if chat_logger and chat_logger.is_paying_user():
                    edit_sweep_comment(
                        (
                            f"Sorry, I could not edit `{e.filename}` as this file is too long."
                            " We are currently working on improved file streaming to address"
                            " this issue.\n"
                        ),
                        -1,
                    )
                else:
                    edit_sweep_comment(
                        (
                            f"Sorry, I could not edit `{e.filename}` as this file is too"
                            " long.\n\nIf this file is incorrect, please describe the desired"
                            " file in the prompt. However, if you would like to edit longer"
                            " files, consider upgrading to [Sweep Pro](https://sweep.dev/) for"
                            " longer context lengths.\n"
                        ),
                        -1,
                    )
                delete_branch = True
                raise e
            except NoFilesException as e:
                logger.info("Sweep could not find files to modify")
                edit_sweep_comment(
                    (
                        "Sorry, Sweep could not find any appropriate files to edit to address"
                        " this issue. If this is a mistake, please provide more context and Sweep"
                        f" will retry!\n\n@{username}, please edit the issue description to"
                        " include more details. You can also ask for help on our community" 
                        " forum: https://community.sweep.dev/"
                    ),
                    -1,
                )
                delete_branch = True
                raise e
            except openai.BadRequestError as e:
                logger.error(traceback.format_exc())
                logger.error(e)
                edit_sweep_comment(
                    (
                        "I'm sorry, but it looks our model has ran out of context length. We're"
                        " trying to make this happen less, but one way to mitigate this is to"
                        " code smaller files. If this error persists report it at"
                        " https://community.sweep.dev/."
                    ),
                    -1,
                )
                posthog.capture(
                    username,
                    "failed",
                    properties={
                        "error": str(e),
                        "trace": traceback.format_exc(),
                        "reason": "Invalid request error / context length",
                        **metadata,
                        "duration": round(time() - on_ticket_start_time),
                    },
                )
                delete_branch = True
                raise e
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(e)
                # title and summary are defined elsewhere
                if len(title + summary) < 60:
                    edit_sweep_comment(
                        (
                            "I'm sorry, but it looks like an error occurred due to" 
                            f" a planning failure. The error message is {str(e)}. Feel free to add more details to the issue description"
                            " so Sweep can better address it. Alternatively, post on our community forum"
                            " for assistance: https://community.sweep.dev/"
                        ),
                        -1,
                    )  
                else:
                    edit_sweep_comment(
                        (
                            "I'm sorry, but it looks like an error has occurred due to"
                            + f" a planning failure. The error message is {str(e)}. Feel free to add more details to the issue description"
                            + " so Sweep can better address it. Alternatively, reach out to Kevin or William for help at"
                            + " https://community.sweep.dev/."
                        ),
                        -1,
                    )
                raise e
            else:
                try:
                    fire_and_forget_wrapper(remove_emoji)(content_to_delete="eyes")
                    fire_and_forget_wrapper(add_emoji)("rocket")
                except SystemExit:
                    raise SystemExit
                except Exception as e:
                    logger.error(e)

            if delete_branch:
                try:
                    if pull_request.branch_name.startswith("sweep"):
                        repo.get_git_ref(
                            f"heads/{pull_request.branch_name}"
                        ).delete()
                    else:
                        raise Exception(
                            f"Branch name {pull_request.branch_name} does not start with sweep/"
                        )
                except Exception as e:
                    logger.error(e)
                    logger.error(traceback.format_exc())
                    logger.info("Deleted branch", pull_request.branch_name)
        except Exception as e:
            posthog.capture(
                username,
                "failed",
                properties={
                    **metadata,
                    "error": str(e),
                    "trace": traceback.format_exc(),
                    "duration": round(time() - on_ticket_start_time),
                },
            )
            raise e
        posthog.capture(
            username,
            "success",
            properties={**metadata, "duration": round(time() - on_ticket_start_time)},
        )
        logger.info("on_ticket success in " + str(round(time() - on_ticket_start_time)))
        return {"success": True}