"""Attribute a PR merge to its assigned run and describe unfinished work."""


def own_merge_head(lease, item, github):
    if (item.kind != "pr" or not item.merged or item.state != "closed"
            or not item.merged_by or item.merged_by.casefold() != lease["actor"].casefold()
            or not item.head or not lease.get("assignment_sha")):
        return None
    head = item.head
    seen = set()
    while head != lease["assignment_sha"]:
        if head == lease.get("own_merge_head"):
            return item.head
        if head in seen:
            return None
        seen.add(head)
        parents = github.commit_parents(head)
        if len(parents) != 2:
            return None
        head = parents[0]
    return item.head


def unfinished_merge(summary):
    warning = "Post-merge work may be unfinished."
    return summary if summary.startswith(warning) else f"{warning} {summary}"


def recover_own_merge(source, receipt, item, github):
    head = source.get("own_merge_head") or receipt.get("own_merge_head")
    if not head and item is not None:
        head = own_merge_head(source, item, github)
    return source | {"own_merge_head": head} if head else source
