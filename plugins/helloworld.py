from stratus.loader import hook


@hook.command()
def hello():
    return "Hello World!"

@hook.command()
def test(user):
    return user
