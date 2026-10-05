"""GitHub bot identity shared by REST and GraphQL input reads."""


def is_bot(actor):
    return (isinstance(actor, dict) and
            (actor.get("type") == "Bot" or actor.get("__typename") == "Bot"))


def listed_bot(actor, logins):
    if not is_bot(actor) or not isinstance(actor.get("login"), str):
        return False
    # REST app logins carry [bot]; GraphQL reports the same login without it.
    login = actor["login"].casefold().removesuffix("[bot]")
    return any(login == configured.casefold().removesuffix("[bot]") for configured in logins)
