"""Read-only board discovery and explicitly staged public discussion creation."""

from dataclasses import dataclass

from .errors import AgentError

MANUAL_BOARDS = "https://docs.github.com/en/discussions/managing-discussions-for-your-community/creating-a-discussion"
BOARD_FIELDS = "id number url title locked closed viewerCanUpdate"


@dataclass(frozen=True)
class BoardRequest:
    title: str
    category: str
    category_id: str
    repository_id: str


def board_body(title):
    return f"# {title}\n\nLessons from completed ub-agents runs. Agents publish retrospectives here as the launcher account.\n"


class Boards:
    def __init__(self, github, repository):
        self.github, self.repository = github, repository
        owner, name = repository.split("/")
        self.variables = {"owner": owner, "name": name}

    def setup(self):
        repo = self.discussions()
        if repo["hasDiscussionsEnabled"] is not True:
            raise AgentError(f"Enable GitHub Discussions in {self.repository}")
        return repo

    def discussions(self):
        data = self.github.graphql(
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
            "id nameWithOwner url hasDiscussionsEnabled viewerPermission "
            "discussionCategories(first:100){nodes{id name} pageInfo{hasNextPage}}}}", self.variables)
        try:
            repo = data["repository"]
            if repo["nameWithOwner"] != self.repository or repo["url"] != f"https://github.com/{self.repository}":
                raise ValueError("repository mismatch")
            if not isinstance(repo["hasDiscussionsEnabled"], bool):
                raise ValueError("invalid Discussions flag")
            if not isinstance(repo["id"], str) or not repo["id"]:
                raise ValueError("missing repository ID")
            if repo["viewerPermission"] not in {None, "READ", "TRIAGE", "WRITE", "MAINTAIN", "ADMIN"}:
                raise ValueError("invalid account role")
            categories = repo["discussionCategories"]
            if not isinstance(categories["nodes"], list):
                raise ValueError("invalid categories")
            if categories["pageInfo"]["hasNextPage"] is not False:
                raise ValueError("incomplete categories")
            if any(not isinstance(c["id"], str) or not c["id"] or
                   not isinstance(c["name"], str) or not c["name"] for c in categories["nodes"]):
                raise ValueError("invalid category")
            return repo
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError(f"Discussions availability or repository identity cannot be verified for {self.repository}") from exc

    def verify(self, number):
        if type(number) is not int or number <= 0:
            raise AgentError("Discussion numbers must be positive integers")
        data = self.github.graphql(
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "discussion(number:$number){" + BOARD_FIELDS + "}}}", self.variables | {"number": number})
        try:
            board = data["repository"]["discussion"]
            if not isinstance(board["id"], str) or not board["id"] or type(board["number"]) is not int or board["number"] != number:
                raise ValueError("missing number or ID")
            if board["url"] != f"https://github.com/{self.repository}/discussions/{number}":
                raise ValueError("URL mismatch")
            # GitHub exposes update capability, not a viewerCanComment field.
            # Require this positive write-capability evidence on an open board;
            # read access or a repository role alone is insufficient.
            if board["locked"] is not False or board["closed"] is not False or board["viewerCanUpdate"] is not True:
                raise AgentError(f"Discussion #{number}: open-board posting/write access cannot be verified; repair Discussions write access")
            return board
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError(f"Discussion #{number} is invalid or unreadable in {self.repository}") from exc

    def find(self, title):
        """Discover exact chosen titles across every page; never guess board numbers."""
        matches, cursor, seen = [], None, set()
        while True:
            data = self.github.graphql(
                "query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){"
                "discussions(first:100,after:$cursor){nodes{number title} pageInfo{hasNextPage endCursor}}}}",
                self.variables | {"cursor": cursor})
            try:
                connection = data["repository"]["discussions"]
                for board in connection["nodes"]:
                    if type(board["number"]) is not int or board["number"] <= 0 or not isinstance(board["title"], str):
                        raise ValueError("invalid discussion")
                    if board["title"] == title:
                        matches.append(board["number"])
                page = connection["pageInfo"]
                if page["hasNextPage"] is False:
                    break
                following = page["endCursor"]
                if page["hasNextPage"] is not True or not isinstance(following, str) or not following or following in seen:
                    raise ValueError("invalid pagination")
                seen.add(following)
                cursor = following
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentError("Board discovery is incomplete") from exc
        if len(matches) > 1:
            raise AgentError(f"Several boards have the title {title!r}; set the board numbers in ub-agents.yaml")
        return self.verify(matches[0]) if matches else None

    def request(self, title, category, repo):
        if repo["viewerPermission"] not in {"WRITE", "MAINTAIN", "ADMIN"}:
            raise AgentError(f"The launcher account cannot create requested boards in {self.repository}; "
                             "grant it Discussions write access")
        chosen = [c for c in repo["discussionCategories"]["nodes"] if c["name"] == category]
        if len(chosen) != 1:
            raise AgentError(f"Discussion category {category!r} is not available")
        return BoardRequest(title, category, chosen[0]["id"], repo["id"])

    def create(self, request):
        data = self.github.graphql(
            "mutation($repositoryId:ID!,$categoryId:ID!,$title:String!,$body:String!){"
            "createDiscussion(input:{repositoryId:$repositoryId,categoryId:$categoryId,title:$title,body:$body}){"
            "discussion{number url}}}",
            {"repositoryId": request.repository_id, "categoryId": request.category_id,
             "title": request.title, "body": board_body(request.title)})
        try:
            board = data["createDiscussion"]["discussion"]
            number = board["number"]
            if type(number) is not int or number <= 0 or board["url"] != f"https://github.com/{self.repository}/discussions/{number}":
                raise ValueError("invalid created destination")
            return board
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError("Board creation response is unverified; inspect the selected title on GitHub before any retry") from exc
