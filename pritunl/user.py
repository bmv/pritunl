from constants import *
from pritunl import app_server
from cache import cache_db
from config import Config
from log_entry import LogEntry
from event import Event
import uuid
import tarfile
import os
import subprocess
import logging
import hashlib
import base64
import utils
import struct
import hmac
import time
import threading

logger = logging.getLogger(APP_NAME)
_openssl_locks = {}

class User(Config):
    str_options = {'name', 'otp_secret', 'type'}
    chmod_mode = 0600
    cached = True
    cache_prefix = 'user'

    def __init__(self, org, id=None, name=None, type=None):
        Config.__init__(self)
        self.org = org

        if id is None:
            if type == CERT_CA:
                self.id = CA_CERT_ID
            elif type is None:
                raise AttributeError('Type must be specified')
            else:
                self.id = uuid.uuid4().hex
            self.name = name
            self.type = type
        else:
            self.id = id

        self.reqs_path = os.path.join(self.org.path, REQS_DIR,
            '%s.csr' % self.id)
        self.ssl_conf_path = os.path.join(self.org.path, TEMP_DIR,
            '%s.conf' % self.id)
        self.key_path = os.path.join(self.org.path, KEYS_DIR,
            '%s.key' % self.id)
        self.cert_path = os.path.join(self.org.path, CERTS_DIR,
            '%s.crt' % self.id)
        self.key_archive_path = os.path.join(self.org.path,
            TEMP_DIR, '%s.tar' % self.id)
        self.set_path(os.path.join(self.org.path, USERS_DIR,
            '%s.conf' % self.id))

        if id not in _openssl_locks:
            _openssl_locks[id] = threading.Lock()

        if id is None:
            self._initialize()

    def dict(self):
        return {
            'id': self.id,
            'organization': self.org.id,
            'organization_name': self.org.name,
            'name': self.name,
            'type': self.type,
            'otp_secret': self.otp_secret,
        }

    def _upgrade_0_10_4(self):
        if not self.type:
            logger.debug('Upgrading user to v0.10.4... %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
            })
            with open(self.cert_path, 'r') as cert_file:
                cert_data = cert_file.read()
                if 'CA:TRUE' in cert_data:
                    self.type = CERT_CA
                elif 'TLS Web Server Authentication' in cert_data:
                    self.type = CERT_SERVER
                else:
                    self.type = CERT_CLIENT
            self.commit()

    def _initialize(self):
        self._create_ssl_conf()
        self._cert_request()
        self._generate_otp_secret()
        self.commit()
        self._cert_create()
        self._delete_ssl_conf()
        if self.type == CERT_CLIENT:
            LogEntry(message='Created new user "%s".' % self.name)
        Event(type=USERS_UPDATED, resource_id=self.org.id)
        Event(type=SERVERS_UPDATED)

    def _cert_request(self):
        _openssl_locks[self.id].acquire()
        try:
            args = [
                'openssl', 'req', '-new', '-batch',
                '-config', self.ssl_conf_path,
                '-out', self.reqs_path,
                '-keyout', self.key_path,
                '-reqexts', '%s_req_ext' % self.type,
            ]
            subprocess.check_call(args, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            logger.exception('Failed to create user cert requests. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
            })
            raise
        finally:
            _openssl_locks[self.id].release()
        os.chmod(self.key_path, 0600)

    def _cert_create(self):
        try:
            args = ['openssl', 'ca', '-batch']
            if self.type == CERT_CA:
                args += ['-selfsign']
            args += [
                '-config', self.ssl_conf_path,
                '-in', self.reqs_path,
                '-out', self.cert_path,
                '-extensions', '%s_ext' % self.type,
            ]
            subprocess.check_call(args, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            logger.exception('Failed to create user cert. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
            })
            raise

    def _create_ssl_conf(self):
        with open(self.ssl_conf_path, 'w') as conf_file:
            conf_file.write(CERT_CONF % (
                self.org.id,
                self.org.path,
                app_server.key_bits,
                self.id,
            ))

    def _delete_ssl_conf(self):
        os.remove(self.ssl_conf_path)

    def _generate_otp_secret(self):
        sha_hash = hashlib.sha512()
        sha_hash.update(os.urandom(8192))
        byte_hash = sha_hash.digest()
        for i in xrange(6):
            sha_hash = hashlib.sha512()
            sha_hash.update(byte_hash)
            byte_hash = sha_hash.digest()
        self.otp_secret = base64.b32encode(byte_hash)[:DEFAULT_OTP_SECRET_LEN]

    def generate_otp_secret(self):
        self._generate_otp_secret()
        self.commit()
        Event(type=USERS_UPDATED, resource_id=self.org.id)

    def verify_otp_code(self, code):
        otp_secret = self.otp_secret
        padding = 8 - len(otp_secret) % 8
        if padding != 8:
            otp_secret = otp_secret.ljust(len(otp_secret) + padding, '=')
        otp_secret = base64.b32decode(otp_secret.upper())
        valid_codes = []
        epoch = int(time.time() / 30)
        for epoch_offset in range(-1, 2):
            value = struct.pack('>q', epoch + epoch_offset)
            hmac_hash = hmac.new(otp_secret, value, hashlib.sha1).digest()
            offset = ord(hmac_hash[-1]) & 0x0F
            truncated_hash = hmac_hash[offset:offset + 4]
            truncated_hash = struct.unpack('>L', truncated_hash)[0]
            truncated_hash &= 0x7FFFFFFF
            truncated_hash %= 1000000
            valid_codes.append('%06d' % truncated_hash)
        if code not in valid_codes:
            return False

        used_codes = cache_db.dict_get_all(self.get_cache_key('otp'))
        for auth_time, used_code in used_codes.items():
            if int(time.time()) - int(auth_time) > 120:
                cache_db.dict_remove(self.get_cache_key('otp'), auth_time)
            if used_code == code:
                return False
        cache_db.dict_set(self.get_cache_key('otp'),
            str(int(time.time())), code)

        return True

    def _revoke(self, reason):
        if self.id == CA_CERT_ID:
            raise TypeError('Cannot revoke ca cert')

        if not os.path.isfile(self.cert_path):
            logger.warning('Skipping revoke of non existent user. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
            })
            return

        _openssl_locks[self.id].acquire()
        try:
            self._create_ssl_conf()
            args = ['openssl', 'ca', '-batch',
                '-config', self.ssl_conf_path,
                '-revoke', self.cert_path,
                '-crl_reason', reason
            ]
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            returncode = proc.wait()
            if returncode != 0:
                err_output = proc.communicate()[1]
                if 'ERROR:Already revoked' not in err_output:
                    raise subprocess.CalledProcessError(returncode, args)
            self._delete_ssl_conf()
        except subprocess.CalledProcessError:
            logger.exception('Failed to revoke user cert. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
            })
            raise
        finally:
            _openssl_locks[self.id].release()
        self.org.generate_crl()

    def _build_key_archive(self):
        user_key_arcname = '%s_%s.key' % (self.org.name, self.name)
        user_cert_arcname = '%s_%s.crt' % (self.org.name, self.name)

        tar_file = tarfile.open(self.key_archive_path, 'w')
        try:
            tar_file.add(self.key_path, arcname=user_key_arcname)
            tar_file.add(self.cert_path, arcname=user_cert_arcname)
            for server in self.org.get_servers():
                server_cert_arcname = '%s_%s_%s.crt' % (
                    self.org.name, self.name, server.name)
                server_conf_path = os.path.join(self.org.path,
                    TEMP_DIR, '%s_%s.ovpn' % (self.id, server.id))
                server_conf_arcname = '%s_%s_%s.ovpn' % (
                    self.org.name, self.name, server.name)
                server.generate_ca_cert()
                tar_file.add(server.ca_cert_path, arcname=server_cert_arcname)

                client_conf = OVPN_CLIENT_CONF % (
                    server.protocol,
                    server.public_address, server.port,
                    server_cert_arcname,
                    user_cert_arcname,
                    user_key_arcname,
                )

                if server.otp_auth:
                    client_conf += 'auth-user-pass\n'

                with open(server_conf_path, 'w') as ovpn_conf:
                    ovpn_conf.write(client_conf)
                tar_file.add(server_conf_path, arcname=server_conf_arcname)
                os.remove(server_conf_path)
        finally:
            tar_file.close()

        return self.key_archive_path

    def _build_inline_key_archive(self):
        tar_file = tarfile.open(self.key_archive_path, 'w')
        try:
            for server in self.org.get_servers():
                server_conf_path = os.path.join(self.org.path,
                    TEMP_DIR, '%s_%s.ovpn' % (self.id, server.id))
                server_conf_arcname = '%s_%s_%s.ovpn' % (
                    self.org.name, self.name, server.name)
                server.generate_ca_cert()

                client_conf = OVPN_INLINE_CLIENT_CONF % (
                    server.protocol,
                    server.public_address, server.port,
                )

                if server.otp_auth:
                    client_conf += 'auth-user-pass\n'

                client_conf += '<ca>\n%s\n</ca>\n' % utils.get_cert_block(
                    server.ca_cert_path)
                client_conf += '<cert>\n%s\n</cert>\n' % utils.get_cert_block(
                    self.cert_path)
                client_conf += '<key>\n%s\n</key>\n' % open(
                    self.key_path).read().strip()

                with open(server_conf_path, 'w') as ovpn_conf:
                    os.chmod(server_conf_path, 0600)
                    ovpn_conf.write(client_conf)
                tar_file.add(server_conf_path, arcname=server_conf_arcname)
                os.remove(server_conf_path)
        finally:
            tar_file.close()

        return self.key_archive_path

    def build_key_archive(self):
        if app_server.inline_certs:
            return self._build_inline_key_archive()
        else:
            return self._build_key_archive()

    def build_key_conf(self, server_id):
        server = self.org.get_server(server_id)
        conf_name = '%s_%s_%s.ovpn' % (self.org.name, self.name, server.name)
        server.generate_ca_cert()

        client_conf = OVPN_INLINE_CLIENT_CONF % (
            server.protocol,
            server.public_address, server.port,
        )

        if server.otp_auth:
            client_conf += 'auth-user-pass\n'

        client_conf += '<ca>\n%s\n</ca>\n' % utils.get_cert_block(
            server.ca_cert_path)
        client_conf += '<cert>\n%s\n</cert>\n' % utils.get_cert_block(
            self.cert_path)
        client_conf += '<key>\n%s\n</key>\n' % open(
            self.key_path).read().strip()

        return {
            'name': conf_name,
            'conf': client_conf,
        }

    def clear_cache(self):
        cache_db.set_remove(self.org.get_cache_key('users'), self.id)
        Config.clear_cache(self)

    def rename(self, name):
        self.name = name
        self.commit()
        Event(type=USERS_UPDATED, resource_id=self.org.id)

    def remove(self, reason=UNSPECIFIED):
        self.clear_cache()
        name = self.name
        type = self.type
        self._revoke(reason)

        try:
            os.remove(self.reqs_path)
        except OSError, error:
            logger.debug('Failed to remove user reqs file. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
                'path': self.reqs_path,
                'error': error,
            })

        try:
            os.remove(self.ssl_conf_path)
        except OSError, error:
            pass

        try:
            os.remove(self.key_path)
        except OSError, error:
            logger.debug('Failed to remove user key file. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
                'path': self.reqs_path,
                'error': error,
            })

        try:
            os.remove(self.cert_path)
        except OSError, error:
            logger.debug('Failed to remove user cert file. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
                'path': self.reqs_path,
                'error': error,
            })

        try:
            os.remove(self.get_path())
        except OSError, error:
            logger.debug('Failed to remove user conf file. %r' % {
                'org_id': self.org.id,
                'user_id': self.id,
                'path': self.reqs_path,
                'error': error,
            })

        Event(type=USERS_UPDATED, resource_id=self.org.id)
        if type == CERT_CLIENT:
            LogEntry(message='Deleted user "%s".' % name)
