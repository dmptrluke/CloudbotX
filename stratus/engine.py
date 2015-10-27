import asyncio
import gc
import logging
import re
import time

import redis

from stratus.event import Event, CommandHookEvent, RegexHookEvent, EventType
from stratus.helpers.config import Config
from stratus.irc.client import IRCClient
from stratus.plugins.loader import Loader

logger = logging.getLogger("bot")


def clean_name(n):
    """strip all spaces and capitalization
    :type n: str
    :rtype: str
    """
    return re.sub('\s+', '', n.lower())


class Stratus:
    """
    :type start_time: float
    :type running: bool
    :type connections: list[Server | IrcConnection]
    :type config: core.config.Config
    :type loader: Loader
    :type db: redis.StrictRedis
    :type loop: asyncio.events.AbstractEventLoop
    :type stopped_future: asyncio.Future
    :param: stopped_future: Future that will be given a result when the bot has stopped.
    """

    def __init__(self):
        # basic variables
        self.loop = asyncio.get_event_loop()
        self.start_time = time.time()
        self.running = True
        # future which will be called when the bot stops
        self.stopped_future = asyncio.Future(loop=self.loop)

        # stores each bot server connection
        self.connections = []

        # set up config
        self.config = Config(self)
        logger.debug("Config system initialised.")

        # setup db
        db_config = self.config.get('database')
        db_host = db_config.get('host', 'localhost')
        db_port = db_config.get('port', 6379)
        db_database = db_config.get('database', 0)
        logger.info("Connecting to redis at {}:{}/{}".format(db_host, db_port, db_database))
        self.db = redis.StrictRedis(host=db_host, port=db_port, db=db_database)
        logger.debug("Database system initialised.")

        # Bot initialisation complete
        logger.debug("Bot setup completed.")

        # create bot connections
        self.create_connections()

        self.loader = Loader(self)

    def run(self):
        """
        Starts Stratus.
        This will load plugins, connect to IRC, and process input.
        :return: True if Stratus should be restarted, False otherwise
        :rtype: bool
        """
        # Initializes the bot, plugins and connections
        self.loop.run_until_complete(self._init_routine())
        # Wait till the bot stops. The stopped_future will be set to True to restart, False otherwise
        self.loop.run_until_complete(self.stopped_future)
        self.loop.close()

    def create_connections(self):
        """ Create a BotConnection for all the networks defined in the config """
        for config in self.config['connections']:
            # strip all spaces and capitalization from the connection name
            name = clean_name(config['name'])
            nick = config['nick']
            server = config['connection']['server']
            port = config['connection'].get('port', 6667)

            self.connections.append(IRCClient(self, name, nick, config=config,
                                              server=server, port=port,
                                              use_ssl=config['connection'].get('ssl', False)))
            logger.debug("[{}] Created connection.".format(name))

    async def stop(self, reason=None):
        """quits all networks and shuts the bot down"""
        logger.info("Stopping.")

        for connection in self.connections:
            if not connection.connected:
                continue

            logger.debug("[{}] Closing connection.".format(connection.name))

            connection.quit(reason)

        await asyncio.sleep(0.5)  # wait for 'QUIT' calls to take affect

        for connection in self.connections:
            if not connection.connected:
                continue

            connection.close()

        await self.loader.run_shutdown_hooks()
        self.running = False
        self.stopped_future.set_result(1)

    async def _init_routine(self):
        # Load plugins
        await self.loader.load_all(self.config.get("plugin_directories", ["plugins"]))

        # If we we're stopped while loading plugins, cancel that and just stop
        if not self.running:
            logger.info("Killed while loading, exiting")
            return

        # Connect to servers
        await asyncio.gather(*[conn.connect() for conn in self.connections], loop=self.loop)

        # Run a manual garbage collection cycle, to clean up any unused objects created during initialization
        gc.collect()

    async def process(self, event):
        """
        :type event: Event
        """
        first = []
        tasks = []
        command_prefix = event.conn.config.get('command_prefix', '.')

        if hasattr(event, 'irc_command'):
            # Raw IRC hook
            for raw_hook in self.loader.catch_all_triggers:
                if raw_hook.run_first:
                    first.append(self.loader.launch(raw_hook, event))
                else:
                    tasks.append(self.loader.launch(raw_hook, event))
            if event.irc_command in self.loader.raw_triggers:
                for raw_hook in self.loader.raw_triggers[event.irc_command]:
                    if raw_hook.run_first:
                        first.append(self.loader.launch(raw_hook, event))
                    else:
                        tasks.append(self.loader.launch(raw_hook, event))

        # Event hooks
        if event.type in self.loader.event_type_hooks:
            for event_hook in self.loader.event_type_hooks[event.type]:
                if event_hook.run_first:
                    first.append(self.loader.launch(event_hook, event))
                else:
                    tasks.append(self.loader.launch(event_hook, event))

        if event.type is EventType.message:
            # Commands
            if event.chan_name.lower() == event.nick.lower():  # private message, no command prefix
                command_re = r'(?i)^(?:[{}]?|{}[,;:]+\s+)([\w-]+)(?:$|\s+)(.*)'.format(command_prefix,
                                                                                       event.conn.bot_nick)
            else:
                command_re = r'(?i)^(?:[{}]|{}[,;:]+\s+)([\w-]+)(?:$|\s+)(.*)'.format(command_prefix,
                                                                                      event.conn.bot_nick)

            match = re.match(command_re, event.content)

            if match:
                command = match.group(1).lower()
                if command in self.loader.commands:
                    command_hook = self.loader.commands[command]
                    command_event = CommandHookEvent(hook=command_hook, text=match.group(2).strip(),
                                                     triggered_command=command, base_event=event)
                    if command_hook.run_first:
                        first.append(self.loader.launch(command_hook, event, command_event))
                    else:
                        tasks.append(self.loader.launch(command_hook, event, command_event))

            # Regex hooks
            for regex, regex_hook in self.loader.regex_hooks:
                match = regex.search(event.content)
                if match:
                    regex_event = RegexHookEvent(hook=regex_hook, match=match, base_event=event)
                    if regex_hook.run_first:
                        first.append(self.loader.launch(regex_hook, event, regex_event))
                    else:
                        tasks.append(self.loader.launch(regex_hook, event, regex_event))

        # Run the tasks
        await asyncio.gather(*first, loop=self.loop)
        await asyncio.gather(*tasks, loop=self.loop)
