from unittest import TestCase
import unittest
from cilantro_ee.messages.block_data.block_data import GENESIS_BLOCK_HASH
from cilantro_ee.storage.state import MetaDataStorage, update_nonce_hash
import json
from cilantro_ee.protocol.wallet import Wallet
from cilantro_ee.protocol.transaction import TransactionBuilder
from cilantro_ee.messages import capnp as schemas
import os
import capnp
import secrets
from cilantro_ee.protocol.structures.merkle_tree import MerkleTree
from contracting.db import encoder

blockdata_capnp = capnp.load(os.path.dirname(schemas.__file__) + '/blockdata.capnp')
subblock_capnp = capnp.load(os.path.dirname(schemas.__file__) + '/subblock.capnp')
envelope_capnp = capnp.load(os.path.dirname(schemas.__file__) + '/envelope.capnp')
transaction_capnp = capnp.load(os.path.dirname(schemas.__file__) + '/transaction.capnp')
signal_capnp = capnp.load(os.path.dirname(schemas.__file__) + '/signals.capnp')


class TestStateDriver(TestCase):
    def setUp(self):
        self.r = MetaDataStorage()  # this is a class, not an instance, so we do not instantiate
        self.r.flush()

    def test_state_updated(self):
        # Generate some random transactions
        transactions = []
        get_sets = []
        for i in range(50):
            w = Wallet()
            tx = TransactionBuilder(w.verifying_key(), contract='currency',
                                    function='transfer',
                                    kwargs={'amount': 10, 'to': 'jeff'},
                                    stamps=500000,
                                    processor=secrets.token_bytes(32),
                                    nonce=0)

            tx.sign(w.signing_key())
            packed_tx = tx.as_struct()

            # Create a hashmap between a key and the value it should be set to randomly
            get_set = {secrets.token_hex(8): secrets.token_hex(8)}
            get_sets.append(get_set)

            # Put this hashmap as the state of the contract execution and contruct it into a capnp struct
            tx_data = transaction_capnp.TransactionData.new_message(
                transaction=packed_tx,
                status='SUCC',
                state=json.dumps(get_set),
                contractType=0
            )

            # Append it to our list
            transactions.append(tx_data)

        # Build a subblock. One will do
        tree = MerkleTree.from_raw_transactions([tx.to_bytes_packed() for tx in transactions])

        w = Wallet()

        sig = w.sign(tree.root)

        sb = subblock_capnp.SubBlock.new_message(
            merkleRoot=tree.root,
            signatures=[sig],
            merkleLeaves=tree.leaves,
            subBlockIdx=0,
            inputHash=b'a' * 32,
            transactions=[tx for tx in transactions]
        )

        import hashlib

        h = hashlib.sha3_256()
        h.update(b'\00' * 32)
        h.update(tree.root)

        block = blockdata_capnp.BlockData.new_message(
            blockHash=h.digest(),
            blockNum=1,
            blockOwners=[b'\00' * 32],
            prevBlockHash=b'\00' * 32,
            subBlocks=[sb]
        )

        self.r.update_with_block(block)

        for kv in get_sets:
            k, v = list(kv.items())[0]
            got = self.r.get(k)
            self.assertEqual(v, got)

    # TODO test this with publish transactions

    def test_get_latest_block_hash_with_none_set(self):
        b_hash = self.r.get_latest_block_hash()
        self.assertEqual(GENESIS_BLOCK_HASH, b_hash)

    def test_get_latest_block_num_with_none_set(self):
        b_num = self.r.get_latest_block_num()
        self.assertEqual(0, b_num)

    def test_set_get_latest_block_hash(self):
        b_hash = b'A' * 32
        self.r.set_latest_block_hash(b_hash)

        self.assertEqual(self.r.get_latest_block_hash(), b_hash)

    def test_set_get_latest_block_num(self):
        b_num = 9001
        self.r.set_latest_block_num(b_num)

        self.assertEqual(self.r.get_latest_block_num(), b_num)

    def test_update_nonce_empty_hash_adds_anything(self):
        sender = b'123'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=999,
        )

        nonces = {}
        update_nonce_hash(nonces, tx_payload)

        self.assertEqual(nonces.get((b'456', b'123')), 999)

    def test_update_nonce_when_new_max_nonce_found(self):
        nonces = {}

        sender = b'123'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=999,
        )

        update_nonce_hash(nonces, tx_payload)

        sender = b'123'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=1000,
        )

        update_nonce_hash(nonces, tx_payload)

        self.assertEqual(nonces.get((b'456', b'123')), 1000)

    def test_update_nonce_only_keeps_max_values(self):
        nonces = {}

        sender = b'123'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=1000,
        )

        update_nonce_hash(nonces, tx_payload)

        sender = b'123'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=999,
        )

        update_nonce_hash(nonces, tx_payload)

        self.assertEqual(nonces.get((b'456', b'123')), 1000)

    def test_update_nonce_keeps_multiple_nonces(self):
        nonces = {}

        sender = b'123'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=1000,
        )

        update_nonce_hash(nonces, tx_payload)

        sender = b'124'
        processor = b'456'
        tx_payload = transaction_capnp.TransactionPayload.new_message(
            sender=sender,
            processor=processor,
            nonce=999,
        )

        update_nonce_hash(nonces, tx_payload)

        self.assertEqual(nonces.get((b'456', b'123')), 1000)
        self.assertEqual(nonces.get((b'456', b'124')), 999)

    def test_set_transaction_data_single_value(self):
        update = {'123': 999}
        encoded = json.dumps(update)
        tx_data = transaction_capnp.TransactionData.new_message(
            state=encoded
        )

        self.r.set_transaction_data(tx=tx_data)

        self.assertEqual(self.r.get('123'), 999)

    def test_set_transaction_multiple_values(self):
        update = {'123': 999,
                  'stu': b'555',
                  'something': [1, 2, 3]}

        encoded = encoder.encode(update)
        tx_data = transaction_capnp.TransactionData.new_message(
            state=encoded
        )

        self.r.set_transaction_data(tx=tx_data)
        self.assertEqual(self.r.get('123'), 999)
        self.assertEqual(self.r.get('stu'), b'555')
        self.assertEqual(self.r.get('something'), [1, 2, 3])
        '''
        struct TransactionData {
    transaction @0 :Transaction;
    status @1: Text;
    state @2: Text;
    contractType @3: UInt16;
}
        :return: 
        '''