from cilantro.protocol.overlay.network import Network
from cilantro.protocol.overlay.discovery import Discovery
from cilantro.protocol.overlay.handshake import Handshake
from cilantro.protocol.overlay.event import Event
from cilantro.logger.base import get_logger
from cilantro.protocol.structures.node import Node
from cilantro.utils.keys import Keys

import asyncio, zmq.asyncio, zmq
import abc


class OverlayInterface2(abc.ABC):
    def __init__(self):
        pass
        # interface dictionary

    @abc.abstractmethod
    def lookup_ip(self, vk):
        pass

    @abc.abstractmethod
    def lookup_and_handshake(self, vk):
        pass

    @abc.abstractmethod
    def handshake_ip(self, ip):
        pass

    @abc.abstractmethod
    def ping_node(self, ip):
        pass

    @abc.abstractmethod
    def track_new_nodes(self):
        pass


class OverlayInterface:
    started = False
    log = get_logger('OverlayInterface')

    def __init__(self, sk_hex, loop=None, ctx=None):

        self.loop = loop or asyncio.get_event_loop()
        # asyncio.set_event_loop(self.loop)
        self.ctx = ctx or zmq.asyncio.Context()
        # reset_auth_folder should always be False and True has to be at highest level without any processes
        Keys.setup(sk_hex=sk_hex)


        self.network = Network(loop=self.loop)
        self.discovery = Discovery(Keys.vk, self.ctx)
        Handshake.setup(loop=self.loop, ctx=self.ctx)
        self.tasks = [
            self.discovery.listen(),
            Handshake.listen(),
            self.network.protocol.listen(),
            self.bootup()
        ]

    def start(self):
        self.loop.run_until_complete(asyncio.gather(
            *self.tasks
        ))

    @property
    def neighbors(self):
        return {item[2]: Node(ip=item[0], port=item[1], vk=item[2]) \
            for item in self.network.bootstrappableNeighbors()}

    @property
    def authorized_nodes(self):
        return Handshake.authorized_nodes

    async def bootup(self):
        addrs = await self.discovery.discover_nodes()
        if addrs:
            await self.network.bootstrap(addrs)
        self.log.success('''
###########################################################################
#   BOOTSTRAP COMPLETE
###########################################################################\
        ''')
        self.started = True
        Event.emit({ 'event': 'service_status', 'status': 'ready' })

    async def authenticate(self, ip, vk, domain='*'):
        return await Handshake.initiate_handshake(ip, vk, domain)

    async def lookup_ip(self, vk):
        return await self.network.lookup_ip(vk)

    def track_new_nodes(self):
        self.network.track_and_inform()

    def teardown(self):
        self.log.important('Shutting Down.')
        for task in self.tasks:
            task.cancel()
        self.started = False
