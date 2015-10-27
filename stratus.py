import asyncio
import logging
import os
import json
import sys
import signal

# we need to make sure we are in the install directory
install_dir = os.path.realpath(os.path.dirname(__file__))
sys.path[0] = os.path.dirname(install_dir)
os.chdir(install_dir)

# import bot
from stratus.engine import Stratus


def main():
    # Logging optimizations, doing it here because we only want to change this if we're the main file
    logging._srcfile = None
    logging.logThreads = 0
    logging.logProcesses = 0
    logging.logMultiprocessing = 0
    logger = logging.getLogger("stratus")

    logger.info("Starting stratus.".format())

    # temporary config shit
    with open("config.json") as f:
        config = json.load(f)

    # create the bot
    bot = Stratus(config)

    # store the original SIGINT handler
    original_sigint = signal.getsignal(signal.SIGINT)

    # define closure for signal handling
    def exit_gracefully(signum, frame):
        bot.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(bot.stop("Killed"), loop=bot.loop))

        # restore the original handler so if they do it again it triggers
        signal.signal(signal.SIGINT, original_sigint)

    signal.signal(signal.SIGINT, exit_gracefully)

    # start the bot master
    bot.run()

    # close logging, and exit the program.
    logger.debug("Stopping logging engine")
    logging.shutdown()


main()
