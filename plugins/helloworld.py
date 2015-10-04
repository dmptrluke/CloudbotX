from stratus.plugins import hook


@hook.command()
def hello():
    return "Hello World!"

