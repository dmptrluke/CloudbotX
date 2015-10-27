import os
import platform
import time
from datetime import timedelta

import psutil

import stratus
from stratus.loader import hook

plugin_info = {
    "plugin_category": "obr",
    "command_category_name": "Informational"
}


def format_bytes(num):
    for x in ['bytes', 'kb', 'mb', 'gb']:
        if num < 1024.0:
            return "%3.1f%s" % (num, x)
        num /= 1024.0
    return "%3.1f%s" % (num, 'TB')


@hook.command(autohelp=False)
def about(conn):
    """-- Gives information about Stratus."""
    return "{} is powered by Stratus! ({}) - " \
           "https://github.com/lukeroge/Stratus/".format(conn.nick, stratus.__version__)

@hook.command(autohelp=False)
def system():
    """-- Retrieves information about the host system."""
    process = psutil.Process(os.getpid())

    # get the data we need using the Process we got
    cpu_usage = process.cpu_percent()
    thread_count = len(process.threads())
    memory_usage = format_bytes(process.memory_info()[0])

    # Get general system info
    sys_os = platform.platform()
    python_implementation = platform.python_implementation()
    python_version = platform.python_version()
    sys_architecture = '-'.join(platform.architecture())
    sys_cpu_count = platform.machine()

    # Get uptime
    uptime = timedelta(seconds=round(time.time() - process.create_time()))

    return (
        "OS: \x02{}\x02, "
        "Python: \x02{} {}\x02, "
        "Architecture: \x02{}\x02 - \x02{}\x02\n"
        "Uptime: \x02{}\x02 "
        "Threads: \x02{}\x02, "
        "CPU Usage: \x02{}\x02, "
        "Memory Usage: \x02{}\x02, "
    ).format(
        sys_os,
        python_implementation,
        python_version,
        sys_architecture,
        sys_cpu_count,
        uptime,
        thread_count,
        cpu_usage,
        memory_usage,
    )
