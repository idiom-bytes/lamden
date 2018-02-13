import asyncio
import zmq
from zmq.asyncio import Context
from cilantro.serialization import JSONSerializer
from cilantro.proofs.pow import SHA3POW
from cilantro.networking.constants import MAX_QUEUE_SIZE
from cilantro.db.transaction_queue_db import TransactionQueueDB
from cilantro.interpreters.basic_interpreter import BasicInterpreter
from cilantro.transactions.testnet import TestNetTransaction
import time
import sys
if sys.platform != 'win32':
    import uvloop


'''
    Delegates

    Delegates are the "miners" of the Cilantro blockchain in that they opportunistically bundle up transactions into 
    blocks and are rewarded with TAU for their actions. They receive approved transactions from delegates and broadcast
    blocks based on a 1 second or 10,000 transaction limit per block. They should be able to connect/drop from the 
    network seamlessly as well as coordinate blocks amongst themselves. 
'''


class Delegate(object):
    def __init__(self, host='127.0.0.1', sub_port='7777', serializer=JSONSerializer, hasher=SHA3POW):
        self.host = host
        self.sub_port = sub_port
        self.serializer = serializer
        self.hasher = hasher
        self.sub_url = 'tcp://{}:{}'.format(self.host, self.sub_port)

        self.ctx = Context()
        self.delegate_sub = self.ctx.socket(socket_type=zmq.SUB)

        self.queue = TransactionQueueDB()
        self.interpreter = BasicInterpreter()

        self.loop = None

    def start_async(self):
        self.loop = asyncio.get_event_loop() # set uvloop here
        self.loop.run_until_complete(self.recv())

    async def recv(self):
        self.delegate_sub.connect(self.sub_url)
        self.delegate_sub.setsockopt(zmq.SUBSCRIBE, b'')

        while True:
            msg_count = 0
            msg = await self.delegate_sub.recv()
            print('received', msg)
            msg_count += 1
            if self.delegate_time() or msg_count == 10000: # conditions for delegate logic go here.
                self.delegate_logic()

    def delegate_logic(self):
        """
        Step 1) Delegate takes 10k transactions from witness and forms a block
        Step 2) Block propagates across the network to other delegates
        Step 3) Delegates pass around in memory DB hash to confirm they have the same blockchain state
        Step 4) Next block is mined and process repeats

        zmq pattern: subscribers (delegates) need to be able to communicate with one another. this can be achieved via
        a push/pull pattern where all delegates push their state to sink that pulls them in, but this is centralized.
        another option is to use ZMQ stream to have the tcp sockets talk to one another outside zmq
        """
        pass

    def process_transaction(self, data: bytes=None):
        """
        Processes a transaction from witness. This first feeds it through the interpreter, and if
        no errors are thrown, then adds the transaction to the queue.
        :param data: The raw transaction data, assumed to be in byte format
        :return:
        """
        d, tx = None, None

        try:
            d = self.serializer.serialize(data)
        except Exception as e:
            print("Error in delegate serializing data -- {}\nRaw data: {}".format(e, data))
            return {'status': 'error in deleate deserializing data: {}\nRaw data: {}'.format(e, data)}

        print("Delegate processing tx: {}".format(d))  # Debug

        try:
            tx = TestNetTransaction.from_dict(d)
        except Exception as e:
            print('Error building transaction from dictionary: {}\nerror = {}'.format(d, e))
            return {'status': 'Error building transaction from dictionary: {}\nerror = {}'.format(d, e)}

        try:
            self.interpreter.interpret_transaction(tx)
        except Exception as e:
            print('Error interpreting transaction: {}\nTransaction dict: {}'.format(e, d))
            return {'status': 'error interpreting transaction: {}'.format(e)}

        self.queue.enqueue_transaction(tx.payload['payload'])

        if self.queue.queue_size() > MAX_QUEUE_SIZE:
            print('queue exceeded max size...delegate performing consensus')
            self.perform_consensus()

    def perform_consensus(self):
        print('delegate performing consensus...')
        pass

    async def delegate_time(self):
        """Conditions to check that 1 second has passed"""
        start_time = time.time()
        await time.sleep(1.0 - ((time.time() - start_time) % 1.0))
        return True