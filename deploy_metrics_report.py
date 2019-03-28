import os
import re
import csv
import time
import requests
import traceback
from datetime import datetime
from datetime import timedelta


class BaseException(Exception):
    def __init__(self, status, data):
        Exception.__init__(self)
        self.__status = status
        self.__data = data
        self.args = [status, data]

    @property
    def status(self):
        return self.__status

    @property
    def data(self):
        return self.__data

    def __str__(self):
        return str(self.__status) + " " + str(self.__data)


class RateLimitExceededException(BaseException):
    pass


class UnauthorizedException(BaseException):
    pass


class UnexpectedException(BaseException):
    pass


class Samson(object):
    SAMSON_API = "https://samson.zende.sk"

    def __init__(self, token, project):
        self.token = token
        self.project = project

    def _api(self, path="", params=""):
        url = "%s/projects/%s.json" % (self.SAMSON_API, self.project)
        if path:
            url = "%s/projects/%s/%s.json" % (self.SAMSON_API, self.project, path)
        if params:
            url += "?%s" % params
        headers = {"Authorization": "Bearer %s" % self.token}

        res = requests.get(url, headers=headers)
        status_code, json_output = res.status_code, res.json()

        if status_code == 401:
            raise UnauthorizedException(status_code, json_output)
        elif status_code >= 400:
            raise UnexpectedException(status_code, json_output)
        
        return json_output

    def get_project(self):
        return self._api()

    def get_deploys_search(self, git_sha="", production=True, page=1):
        params = "inlines=previous_commit&search[git_sha]=%s&search[production]=%s&search[status]=succeeded&commit=Search&page=%s" % (git_sha, str(production).lower(), page)
        return self._api("deploys", params)

    def get_first_deploy(self, git_sha, production=True):
        deploys = self.get_deploys_search(git_sha, production)["deploys"]
        first_deploy = None
        page = 1
        while deploys:
            first_deploy = deploys[-1]
            page += 1
            deploys = self.get_deploys_search(git_sha, production, page)["deploys"]

        return first_deploy

    def get_first_production_deploys_within_date_range(self, from_date, to_date):
        deploys = self.get_deploys_search()["deploys"]
        page = 1
        commit_cache = {}
        while deploys:
            for deploy in deploys:
                created_at = datetime.strptime(deploy["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
                commit = deploy["commit"]
                previous_commit = deploy["previous_commit"]

                if created_at < kwargs["from_date"]:
                    break

                while kwargs["to_date"] < created_at:
                    continue

                if commit_cache.get(commit) or not previous_commit or previous_commit == commit:
                    continue

                first_production_deploy = self.get_first_deploy(commit)
                commit_cache[commit] = True
                yield first_production_deploy, previous_commit, commit

            page += 1
            deploys = self.get_deploys_search("", True, page)["deploys"]


class Github(object):
    GITHUB_API = "https://api.github.com"
    PR_REGEX = re.compile(r"Merge pull request #(\d+)")
    PR_SQUASH_REGEX = re.compile(r".*\(#(\d+)\)")

    def __init__(self, token, repo):
        self.token = token
        self.repo = repo

    def _api(self, path):
        url = "%s/repos/%s/%s" % (self.GITHUB_API, self.repo, path)
        headers = {"Authorization": "token %s" % self.token}

        res = requests.get(url, headers=headers)
        status_code, json_output = res.status_code, res.json()

        if status_code == 403 and json_output["message"].lower().startswith("api rate limit exceeded"):
            raise RateLimitExceededException(status_code, json_output)
        elif status_code >= 400:
            raise UnexpectedException(status_code, json_output)
        
        return json_output

    def compare(self, previous_commit, commit):
        path = "compare/%s...%s" % (previous_commit, commit)
        return self._api(path)

    def get_pull_request(self, pull_request_number):
        path = "pulls/%s" % pull_request_number
        return self._api(path)

    def get_pull_requests_number(self, previous_commit, commit):
        commits = self.compare(previous_commit, commit).get("commits", [])
        for commit in commits:
            message = commit["commit"]["message"]
        
            match = self.PR_REGEX.match(message)
            if match:
                yield match.group(1)
                
            match = self.PR_SQUASH_REGEX.match(message)
            if match:
                yield match.group(1)


def pr_production_time(github, deploy_end_time, previous_commit, commit):
    total = 0
    count = 0
    for pull_request_number in github.get_pull_requests_number(previous_commit, commit):
        pull_request = github.get_pull_request(pull_request_number)
        pull_request_created_at = datetime.strptime(pull_request["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        total += (deploy_end_time - pull_request_created_at).total_seconds()
        count += 1

    if count > 0:
        return total / count

    return 0


def staging_production_time(samson, deploy_end_time, git_sha):
    first_staging_deploy = samson.get_first_deploy(git_sha, False)
    if first_staging_deploy:
        first_staging_deploy_created_at = datetime.strptime(first_staging_deploy["updated_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        return (deploy_end_time - first_staging_deploy_created_at).total_seconds()

    return 0


def main(**kwargs):
    try:
        samson = Samson(kwargs["samson_token"], kwargs["samson_repo"])
        project = samson.get_project()["project"]

        github_repo = project["repository_path"]
        github = Github(kwargs["github_token"], github_repo)

        filename = "deploymetrics_%s_%s.csv" % (samson.project, int(time.time()))
        print "Generating %s ..." % filename
        print "==============================================================================="

        with open(filename, "a") as w:
            writer = csv.writer(w)
            header = ["Deploy ID", "Commit", "PR - Production Cycle Time", "Staging - Production Cycle Time"]
            writer.writerow(header)

            for first_production_deploy, previous_commit, commit in samson.get_first_production_deploys_within_date_range(kwargs["from_date"], kwargs["to_date"]):
                deploy_end_time = datetime.strptime(first_production_deploy["updated_at"], "%Y-%m-%dT%H:%M:%S.%fZ")

                pr_production_cycle_time = pr_production_time(github, deploy_end_time, previous_commit, commit)
                staging_production_cycle_time = staging_production_time(samson, deploy_end_time, commit)

                writer.writerow([first_production_deploy["id"], str(commit), pr_production_cycle_time, staging_production_cycle_time])
                print "Deploy ID: %s generated" % first_production_deploy["id"]

        print "==============================================================================="
        print "DONE!!"
        os.system("open %s" % filename)
    except Exception:
        print traceback.format_exc()


if __name__ == "__main__":
    print "==============================================================================="
    while True:
        samson_token = raw_input("Your samson token: ")
        if len(samson_token) > 0:
            break

    while True:
        samson_repo = raw_input("Project permalink in samson: ")
        if len(samson_repo) > 0:
            break

    while True:
        github_token = raw_input("Your github token: ")
        if len(github_token) > 0:
            break

    today = datetime.today()

    while True:
        three_months_ago = (today - timedelta(3 * 365 / 12))
        from_date = raw_input("From date(yyyy-mm-dd). Default 3 months ago (%s): " % three_months_ago.strftime("%Y-%m-%d"))
        if len(from_date) > 0:
            try:
                from_date = datetime.strptime(from_date, "%Y-%m-%d")
                break
            except ValueError:
                print "Wrong format"
        else:
            from_date = three_months_ago
            break

    while True:
        to_date = raw_input("To date(yyyy-mm-dd). Default today (%s): " % today.strftime("%Y-%m-%d"))
        if len(to_date) > 0:
            try:
                to_date = datetime.strptime(to_date, "%Y-%m-%d")
                break
            except ValueError:
                print "Wrong format"
        else:
            to_date = today
            break
    print "==============================================================================="

    kwargs = {
        "samson_token": samson_token,
        "samson_repo": samson_repo,
        "github_token": github_token,
        "from_date": from_date,
        "to_date": to_date
    }

    main(**kwargs)
