"""
create_pr is a function that creates a pull request from a list of file change requests.
It is also responsible for handling Sweep config PR creation. test
"""

import copy
import datetime

import openai
from github.Repository import Repository
from loguru import logger

from sweepai.agents.modify import modify
from sweepai.config.client import DEFAULT_RULES_STRING, SweepConfig
from sweepai.config.server import (
    ENV,
    GITHUB_BOT_USERNAME,
    GITHUB_CONFIG_BRANCH,
    GITHUB_DEFAULT_CONFIG,
    GITHUB_LABEL_NAME,
)
from sweepai.core.entities import (
    FileChangeRequest,
    MaxTokensExceeded,
)
from sweepai.core.sweep_bot import SweepBot
from sweepai.utils.chat_logger import ChatLogger
from sweepai.utils.event_logger import posthog
from sweepai.utils.github_utils import ClonedRepo, get_github_client

num_of_snippets_to_query = 10
max_num_of_snippets = 5

INSTRUCTIONS_FOR_REVIEW = """\
### 💡 To get Sweep to edit this pull request, you can:
* Comment below, and Sweep can edit the entire PR
* Comment on a file, Sweep will only modify the commented file
* Edit the original issue to get Sweep to recreate the PR from scratch"""

# this should be the only modification function
def handle_file_change_requests(
    file_change_requests: list[FileChangeRequest],
    branch_name: str,
    request: str,
    sweep_bot: SweepBot,
    username: str,
    installation_id: int,
    chat_logger: ChatLogger = None,
    previous_modify_files_dict: dict = {},
):
    sweep_bot.chat_logger = chat_logger
    organization, repo_name = sweep_bot.repo.full_name.split("/")
    metadata = {
        "repo_full_name": sweep_bot.repo.full_name,
        "organization": organization,
        "repo_name": repo_name,
        "repo_description": sweep_bot.repo.description,
        "username": username,
        "installation_id": installation_id,
        "function": "create_pr",
        "mode": ENV,
    }
    posthog.capture(username, "started", properties=metadata)

    try:
        completed_count, fcr_count = 0, len(file_change_requests)

        relevant_filepaths = []
        for file_change_request in file_change_requests:
            if file_change_request.relevant_files:
                # keep all relevant_filepaths
                for file_path in file_change_request.relevant_files:
                    relevant_filepaths.append(file_path)
        # actual modification logic
        modify_files_dict = modify(
            fcrs=file_change_requests,
            request=request,
            cloned_repo=sweep_bot.cloned_repo,
            relevant_filepaths=relevant_filepaths,
            previous_modify_files_dict=previous_modify_files_dict,
        )
        # If no files were updated, log a warning and return
        if not modify_files_dict:
            logger.warning(
                "No changes made to any file!"
            )
            return (
                modify_files_dict,
                False,
                file_change_requests,
            )
        
        # update previous_modify_files_dict
        if not previous_modify_files_dict:
            previous_modify_files_dict = {}
        if modify_files_dict:
            for file_name, file_content in modify_files_dict.items():
                previous_modify_files_dict[file_name] = copy.deepcopy(file_content)
                # update status of corresponding fcr to be succeeded
                for file_change_request in file_change_requests:
                    if file_change_request.filename == file_name:
                        file_change_request.status = "succeeded"

        completed_count = len(modify_files_dict or [])
        logger.info(f"Completed {completed_count}/{fcr_count} files")
        if completed_count == 0 and fcr_count != 0:
            logger.info("No changes made")
            posthog.capture(
                username,
                "failed",
                properties={
                    "error": "No changes made",
                    "reason": "No changes made",
                    **metadata,
                },
            )
        return modify_files_dict, True, file_change_requests
    except MaxTokensExceeded as e:
        logger.error(e)
        posthog.capture(
            username,
            "failed",
            properties={
                "error": str(e),
                "reason": "Max tokens exceeded",
                **metadata,
            },
        )
        raise e
    except openai.BadRequestError as e:
        logger.error(e)
        posthog.capture(
            username,
            "failed",
            properties={
                "error": str(e),
                "reason": "Invalid request error / context length",
                **metadata,
            },
        )
        raise e
    except Exception as e:
        logger.error(e)
        posthog.capture(
            username,
            "failed",
            properties={
                "error": str(e),
                "reason": "Unexpected error",
                **metadata,
            },
        )
        raise e

def safe_delete_sweep_branch(
    pr,  # Github PullRequest
    repo: Repository,
) -> bool:
    """
    Safely delete Sweep branch
    1. Only edited by Sweep
    2. Prefixed by sweep/
    """
    pr_commits = pr.get_commits()
    pr_commit_authors = set([commit.author.login for commit in pr_commits])

    # Check if only Sweep has edited the PR, and sweep/ prefix
    if (
        len(pr_commit_authors) == 1
        and GITHUB_BOT_USERNAME in pr_commit_authors
        and pr.head.ref.startswith("sweep")
    ):
        branch = repo.get_git_ref(f"heads/{pr.head.ref}")
        # pr.edit(state='closed')
        branch.delete()
        return True
    else:
        # Failed to delete branch as it was edited by someone else
        return False


def create_config_pr(
    sweep_bot: SweepBot | None, repo: Repository = None, cloned_repo: ClonedRepo = None
):
    if repo is not None:
        # Check if file exists in repo
        try:
            repo.get_contents("sweep.yaml")
            return
        except SystemExit:
            raise SystemExit
        except Exception:
            pass

    title = "Configure Sweep"
    branch_name = GITHUB_CONFIG_BRANCH
    if sweep_bot is not None:
        branch_name = sweep_bot.create_branch(branch_name, retry=False)
        try:
            # commit_history = []
            # if cloned_repo is not None:
            #     commit_history = cloned_repo.get_commit_history(
            #         limit=1000, time_limited=False
            #     )
            # commit_string = "\n".join(commit_history)

            # sweep_yaml_bot = SweepYamlBot()
            # generated_rules = sweep_yaml_bot.get_sweep_yaml_rules(
            #     commit_history=commit_string
            # )

            sweep_bot.repo.create_file(
                "sweep.yaml",
                "Create sweep.yaml",
                GITHUB_DEFAULT_CONFIG.format(
                    branch=sweep_bot.repo.default_branch,
                    additional_rules=DEFAULT_RULES_STRING,
                ),
                branch=branch_name,
            )
            sweep_bot.repo.create_file(
                ".github/ISSUE_TEMPLATE/sweep-template.yml",
                "Create sweep template",
                SWEEP_TEMPLATE,
                branch=branch_name,
            )
        except SystemExit:
            raise SystemExit
        except Exception as e:
            logger.error(e)
    else:
        # Create branch based on default branch
        repo.create_git_ref(
            ref=f"refs/heads/{branch_name}",
            sha=repo.get_branch(repo.default_branch).commit.sha,
        )

        try:
            # commit_history = []
            # if cloned_repo is not None:
            #     commit_history = cloned_repo.get_commit_history(
            #         limit=1000, time_limited=False
            #     )
            # commit_string = "\n".join(commit_history)

            # sweep_yaml_bot = SweepYamlBot()
            # generated_rules = sweep_yaml_bot.get_sweep_yaml_rules(
            #     commit_history=commit_string
            # )

            repo.create_file(
                "sweep.yaml",
                "Create sweep.yaml",
                GITHUB_DEFAULT_CONFIG.format(
                    branch=repo.default_branch, additional_rules=DEFAULT_RULES_STRING
                ),
                branch=branch_name,
            )
            repo.create_file(
                ".github/ISSUE_TEMPLATE/sweep-template.yml",
                "Create sweep template",
                SWEEP_TEMPLATE,
                branch=branch_name,
            )
        except SystemExit:
            raise SystemExit
        except Exception as e:
            logger.error(e)
    repo = sweep_bot.repo if sweep_bot is not None else repo
    # Check if the pull request from this branch to main already exists.
    # If it does, then we don't need to create a new one.
    if repo is not None:
        pull_requests = repo.get_pulls(
            state="open",
            sort="created",
            base=SweepConfig.get_branch(repo)
            if sweep_bot is not None
            else repo.default_branch,
            head=branch_name,
        )
        for pr in pull_requests:
            if pr.title == title:
                return pr

    logger.print("Default branch", repo.default_branch)
    logger.print("New branch", branch_name)
    pr = repo.create_pull(
        title=title,
        body="""🎉 Thank you for installing Sweep! We're thrilled to announce the latest update for Sweep, your AI junior developer on GitHub. This PR creates a `sweep.yaml` config file, allowing you to personalize Sweep's performance according to your project requirements.

        ## What's new?
        - **Sweep is now configurable**.
        - To configure Sweep, simply edit the `sweep.yaml` file in the root of your repository.
        - If you need help, check out the [Sweep Default Config](https://github.com/sweepai/sweep/blob/main/sweep.yaml) or [Join Our Discourse](https://community.sweep.dev/) for help.

        If you would like me to stop creating this PR, go to issues and say "Sweep: create an empty `sweep.yaml` file".
        Thank you for using Sweep! 🧹""".replace(
            "    ", ""
        ),
        head=branch_name,
        base=SweepConfig.get_branch(repo)
        if sweep_bot is not None
        else repo.default_branch,
    )
    pr.add_to_labels(GITHUB_LABEL_NAME)
    return pr


def add_config_to_top_repos(installation_id, username, repositories, max_repos=3):
    user_token, g = get_github_client(installation_id)

    repo_activity = {}
    for repo_entity in repositories:
        repo = g.get_repo(repo_entity.full_name)
        # instead of using total count, use the date of the latest commit
        commits = repo.get_commits(
            author=username,
            since=datetime.datetime.now() - datetime.timedelta(days=30),
        )
        # get latest commit date
        commit_date = datetime.datetime.now() - datetime.timedelta(days=30)
        for commit in commits:
            if commit.commit.author.date > commit_date:
                commit_date = commit.commit.author.date

        # since_date = datetime.datetime.now() - datetime.timedelta(days=30)
        # commits = repo.get_commits(since=since_date, author="lukejagg")
        repo_activity[repo] = commit_date
        # print(repo, commits.totalCount)
        logger.print(repo, commit_date)

    sorted_repos = sorted(repo_activity, key=repo_activity.get, reverse=True)
    sorted_repos = sorted_repos[:max_repos]

    # For each repo, create a branch based on main branch, then create PR to main branch
    for repo in sorted_repos:
        try:
            logger.print("Creating config for", repo.full_name)
            create_config_pr(
                None,
                repo=repo,
                cloned_repo=ClonedRepo(
                    repo_full_name=repo.full_name,
                    installation_id=installation_id,
                    token=user_token,
                ),
            )
        except SystemExit:
            raise SystemExit
        except Exception as e:
            logger.print(e)
    logger.print("Finished creating configs for top repos")


def create_gha_pr(g, repo):
    # Create a new branch
    branch_name = "sweep/gha-enable"
    repo.create_git_ref(
        ref=f"refs/heads/{branch_name}",
        sha=repo.get_branch(repo.default_branch).commit.sha,
    )

    # Update the sweep.yaml file in this branch to add "gha_enabled: True"
    sweep_yaml_content = (
        repo.get_contents("sweep.yaml", ref=branch_name).decoded_content.decode()
        + "\ngha_enabled: True"
    )
    repo.update_file(
        "sweep.yaml",
        "Enable GitHub Actions",
        sweep_yaml_content,
        repo.get_contents("sweep.yaml", ref=branch_name).sha,
        branch=branch_name,
    )

    # Create a PR from this branch to the main branch
    pr = repo.create_pull(
        title="Enable GitHub Actions",
        body="This PR enables GitHub Actions for this repository.",
        head=branch_name,
        base=repo.default_branch,
    )
    return pr


SWEEP_TEMPLATE = """\
name: Sweep Issue
title: 'Sweep: '
description: For small bugs, features, refactors, and tests to be handled by Sweep, an AI-powered junior developer.
labels: sweep
body:
  - type: textarea
    id: description
    attributes:
      label: Details
      description: Tell Sweep where and what to edit and provide enough context for a new developer to the codebase
      placeholder: |
        Unit Tests: Write unit tests for <FILE>. Test each function in the file. Make sure to test edge cases.
        Bugs: The bug might be in <FILE>. Here are the logs: ...
        Features: the new endpoint should use the ... class from <FILE> because it contains ... logic.
        Refactors: We are migrating this function to ... version because ...
  - type: input
    id: branch
    attributes:
      label: Branch
      description: The branch to work off of (optional)
      placeholder: |
        main"""
