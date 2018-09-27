#!/usr/bin/env python

"""
Generate client and server CURVE certificate files then move them into the
appropriate store directory, private_keys or authorized_keys. The certificates
generated by this script are used by the stonehouse and ironhouse examples.
In practice this would be done by hand or some out-of-band process.
Author: Chris Laws
"""

import os, shutil, datetime
import asyncio, zmq
import zmq.auth, zmq.asyncio
from os.path import basename, splitext, join, exists
from zmq.auth.thread import ThreadAuthenticator
from zmq.auth.asyncio import AsyncioAuthenticator
from zmq.utils.z85 import decode, encode
from nacl.public import PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey
from nacl.bindings import crypto_sign_ed25519_sk_to_curve25519
from cilantro.storage.db import VKBook
from cilantro.constants.overlay_network import AUTH_TIMEOUT
from cilantro.protocol.overlay.utils import digest
from cilantro.logger import get_logger

log = get_logger(__name__)

class Ironhouse:

    auth_port = os.getenv('AUTH_PORT', 4523)
    keyname = os.getenv('HOST_NAME', 'ironhouse')
    authorized_nodes = {}
    base_dir = 'certs/{}'.format(keyname)
    keys_dir = join(base_dir, 'certificates')
    authorized_keys_dir = join(base_dir, 'authorized_keys')
    ctx = None
    auth = None
    daemon_auth = None

    def __init__(self, sk=None, auth_validate=None, wipe_certs=False, auth_port=None, keyname=None, *args, **kwargs):
        if auth_validate: self.auth_validate = auth_validate
        else: self.auth_validate = Ironhouse.auth_validate
        self.auth_port = auth_port or self.auth_port
        self.keyname = keyname or Ironhouse.keyname
        self.pk2vk = {}
        self.vk, self.public_key, self.secret = self.generate_certificates(sk)

    @classmethod
    def vk2pk(cls, vk):
        return encode(VerifyKey(bytes.fromhex(vk)).to_curve25519_public_key()._public_key)

    @classmethod
    def get_public_keys(cls, sk_hex):
        sk = SigningKey(seed=bytes.fromhex(sk_hex))
        vk = sk.verify_key.encode().hex()
        public_key = cls.vk2pk(vk)
        return vk, public_key

    @classmethod
    def generate_certificates(cls, sk_hex, custom_folder=None):
        sk = SigningKey(seed=bytes.fromhex(sk_hex))
        vk = sk.verify_key.encode().hex()
        public_key = cls.vk2pk(vk)
        keyname = decode(public_key).hex()
        private_key = crypto_sign_ed25519_sk_to_curve25519(sk._signing_key).hex()
        authorized_keys_dir = custom_folder or cls.authorized_keys_dir
        for d in [cls.keys_dir, authorized_keys_dir]:
            if exists(d):
                shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)

        secret = None

        _, secret = cls.create_from_private_key(private_key, keyname)

        for key_file in os.listdir(cls.keys_dir):
            if key_file.endswith(".key"):
                shutil.move(join(cls.keys_dir, key_file),
                            join(authorized_keys_dir, '.'))

        if exists(cls.keys_dir):
            shutil.rmtree(cls.keys_dir)

        log.info('Generated CURVE certificate files!')

        return vk, public_key, secret

    @classmethod
    def create_from_private_key(cls, private_key, keyname):
        priv = PrivateKey(bytes.fromhex(private_key))
        publ = priv.public_key
        public_key = encode(publ._public_key)
        secret = encode(priv._private_key)

        base_filename = join(cls.keys_dir, keyname)
        public_key_file = "{0}.key".format(base_filename)
        now = datetime.datetime.now()

        zmq.auth.certs._write_key_file(public_key_file,
                        zmq.auth.certs._cert_public_banner.format(now),
                        public_key)

        return public_key, secret

    def add_public_key(self, public_key, domain='*'):
        if self.public_key == public_key: return
        keyname = decode(public_key).hex()
        authorized_keys_dir = join(self.base_dir, domain) if domain != '*' else self.authorized_keys_dir
        base_filename = join(authorized_keys_dir, keyname)
        public_key_file = "{0}.key".format(base_filename)
        now = datetime.datetime.now()
        if exists(public_key_file):
            log.debug('Public cert for {} has already been created.'.format(public_key))
            self.reconfigure_curve(domain=domain)
            return

        os.makedirs(authorized_keys_dir, exist_ok=True)
        log.info('Adding new public key cert {} to the system.'.format(public_key))
        zmq.auth.certs._write_key_file(public_key_file,
                        zmq.auth.certs._cert_public_banner.format(now),
                        public_key)

        log.debug('{} has added {} to its authorized list under "{}"'.format(os.getenv('HOST_IP', '127.0.0.1'), public_key, domain))
        self.reconfigure_curve(domain=domain)

    def remove_public_key(self, public_key, domain='*'):
        if self.public_key == public_key: return
        keyname = decode(public_key).hex()
        authorized_keys_dir = join(self.base_dir, domain) if domain != '*' else self.authorized_keys_dir
        base_filename = join(authorized_keys_dir, keyname)
        public_key_file = "{0}.key".format(base_filename)

        if exists(public_key_file):
            os.remove(public_key_file)

        log.debug('{} has remove {} from its authorized list'.format(os.getenv('HOST_IP', '127.0.0.1'), public_key))
        self.reconfigure_curve(domain=domain)

    @classmethod
    def secure_context(cls, context=None, async=False):
        if async:
            ctx = context or zmq.asyncio.Context()
            auth = AsyncioAuthenticator(ctx)
            auth.log = log # The constructor doesn't have "log" like its synchronous counter-part
        else:
            ctx = context or zmq.Context()
            auth = ThreadAuthenticator(ctx, log=log)
        auth.start()
        return ctx, auth

    def reconfigure_curve(self, auth=None, domain='*'):
        log.debug('{} is reconfiguring curves'.format(os.getenv('HOST_IP', '127.0.0.1')))
        location = self.authorized_keys_dir if domain == '*' else join(self.base_dir, domain)
        if auth:
            auth.configure_curve(domain=domain, location=location)
        elif self.daemon_auth:
            self.daemon_auth.configure_curve(domain=domain, location=self.authorized_keys_dir)

    @classmethod
    def secure_socket(cls, sock, secret, public_key, curve_serverkey=None):
        sock.curve_secretkey = secret
        sock.curve_publickey = public_key
        if curve_serverkey:
            sock.curve_serverkey = curve_serverkey
        else: sock.curve_server = True
        return sock

    async def authenticate(self, target_public_key, ip, port=None, domain='*'):
        if target_public_key == self.public_key: return 'authorized'
        try:
            PublicKey(decode(target_public_key))
        except Exception as e:
            log.debug('Invalid public key')
            return 'invalid'
        server_url = 'tcp://{}:{}'.format(ip, port or self.auth_port)
        log.debug('{} sending handshake to {}...'.format(os.getenv('HOST_IP', '127.0.0.1'), server_url))
        client = self.ctx.socket(zmq.REQ)
        client = self.secure_socket(client, self.secret, self.public_key, target_public_key)
        client.connect(server_url)
        client.send_multipart([self.vk.encode(), os.getenv('HOST_IP', '127.0.0.1').encode(), domain.encode()])
        authorized = 'unauthorized'

        try:
            msg = await asyncio.wait_for(client.recv(), AUTH_TIMEOUT)
            msg = msg.decode()
            log.debug('{} got secure reply {}, {}'.format(os.getenv('HOST_IP', '127.0.0.1'), msg, target_public_key))
            received_public_key = self.vk2pk(msg)
            if self.auth_validate(msg) == True and target_public_key == received_public_key:
                self.add_public_key(received_public_key, domain=domain)
                self.authorized_nodes[digest(msg)] = ip
                log.debug('{}\'s New Authorized list: {}'.format(os.getenv('HOST_IP', '127.0.0.1'), list(self.authorized_nodes.values())))

                self.pk2vk[received_public_key] = msg
                authorized = 'authorized'
        except Exception as e:
            log.debug('{} got no reply from {} after waiting...'.format(os.getenv('HOST_IP', '127.0.0.1'), server_url))
            authorized = 'no_reply'

        client.disconnect(server_url)
        client.close()

        return authorized

    def setup_secure_server(self):
        self.ctx, self.auth = self.secure_context(async=True)
        self.daemon_context, self.daemon_auth = self.secure_context(async=True)
        self.auth.configure_curve(domain='*', location=zmq.auth.CURVE_ALLOW_ANY)
        self.sec_sock = self.secure_socket(self.ctx.socket(zmq.REP), self.secret, self.public_key)
        self.sec_sock.bind('tcp://*:{}'.format(self.auth_port))
        self.server = asyncio.ensure_future(self.secure_server())

    def cleanup(self):
        if not self.auth._AsyncioAuthenticator__task.done():
            self.auth.stop()
        if not self.daemon_auth._AsyncioAuthenticator__task.done():
            self.daemon_auth.stop()
        self.server.cancel()
        self.sec_sock.close()
        log.info('Ironhouse cleaned up properly.')

    async def secure_server(self):
        log.info('Listening to secure connections at {}'.format(self.auth_port))
        try:
            while True:
                received_vk, received_ip, domain = await self.sec_sock.recv_multipart()
                received_vk = received_vk.decode()
                received_ip = received_ip.decode()
                domain = domain.decode()

                log.debug('{} got secure request {} from user claiming to be "{}"'.format(
                    os.getenv('HOST_IP', '127.0.0.1'), received_vk, received_ip))

                if self.auth_validate(received_vk) == True:
                    public_key = self.vk2pk(received_vk)
                    self.add_public_key(public_key, domain)
                    self.authorized_nodes[digest(received_vk)] = received_ip
                    self.pk2vk[public_key] = received_vk
                    log.debug('{} sending secure reply: {}'.format(os.getenv('HOST_IP', '127.0.0.1'), self.vk))
                    log.debug('{}\'s New Authorized list: {}'.format(os.getenv('HOST_IP', '127.0.0.1'), list(self.authorized_nodes.values())))
                    self.sec_sock.send(self.vk.encode())
                else:
                    log.warning('Unauthorized user {}({})'.format(received_ip, received_vk))
        except Exception as e:
            log.fatal("Got exception in secure server!!! Err={}".format(e))
            raise e
        finally:
            self.cleanup()

    @staticmethod
    def auth_validate(vk):
        return vk in VKBook.get_all()
