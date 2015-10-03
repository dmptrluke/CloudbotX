from stratus import hook


@hook.command()
def hello():
    return "Hello World!"

