"""Tests for acme.standalone."""
import multiprocessing
import os
import shutil
import socket
import threading
import tempfile
import unittest
import warnings
import time
from contextlib import closing

from six.moves import http_client  # pylint: disable=import-error
from six.moves import socketserver  # type: ignore  # pylint: disable=import-error

import josepy as jose
import mock
import requests

from acme import challenges
from acme import crypto_util
from acme import errors
from acme import test_util
from acme.magic_typing import Set # pylint: disable=unused-import, no-name-in-module


class TLSServerTest(unittest.TestCase):
    """Tests for acme.standalone.TLSServer."""


    def test_bind(self):  # pylint: disable=no-self-use
        from acme.standalone import TLSServer
        server = TLSServer(
            ('', 0), socketserver.BaseRequestHandler, bind_and_activate=True)
        server.server_close()

    def test_ipv6(self):
        if socket.has_ipv6:
            from acme.standalone import TLSServer
            server = TLSServer(
                ('', 0), socketserver.BaseRequestHandler, bind_and_activate=True, ipv6=True)
            server.server_close()


class TLSSNI01ServerTest(unittest.TestCase):
    """Test for acme.standalone.TLSSNI01Server."""


    def setUp(self):
        self.certs = {b'localhost': (
            test_util.load_pyopenssl_private_key('rsa2048_key.pem'),
            test_util.load_cert('rsa2048_cert.pem'),
        )}
        from acme.standalone import TLSSNI01Server
        self.server = TLSSNI01Server(('localhost', 0), certs=self.certs)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()

    def test_it(self):
        host, port = self.server.socket.getsockname()[:2]
        cert = crypto_util.probe_sni(
            b'localhost', host=host, port=port, timeout=1)
        self.assertEqual(jose.ComparableX509(cert),
                         jose.ComparableX509(self.certs[b'localhost'][1]))


class BaseRequestHandlerWithLoggingTest(unittest.TestCase):
    """Test for acme.standalone.BaseRequestHandlerWithLogging."""

    def test_it(self):
        with mock.patch('acme.standalone.warnings.warn') as mock_warn:
            # pylint: disable=unused-variable
            from acme.standalone import BaseRequestHandlerWithLogging
        self.assertTrue(mock_warn.called)
        msg = mock_warn.call_args[0][0]
        self.assertTrue(msg.startswith('BaseRequestHandlerWithLogging'))


class HTTP01ServerTest(unittest.TestCase):
    """Tests for acme.standalone.HTTP01Server."""


    def setUp(self):
        self.account_key = jose.JWK.load(
            test_util.load_vector('rsa1024_key.pem'))
        self.resources = set() # type: Set

        from acme.standalone import HTTP01Server
        self.server = HTTP01Server(('', 0), resources=self.resources)

        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()

    def test_index(self):
        response = requests.get(
            'http://localhost:{0}'.format(self.port), verify=False)
        self.assertEqual(
            response.text, 'ACME client standalone challenge solver')
        self.assertTrue(response.ok)

    def test_404(self):
        response = requests.get(
            'http://localhost:{0}/foo'.format(self.port), verify=False)
        self.assertEqual(response.status_code, http_client.NOT_FOUND)

    def _test_http01(self, add):
        chall = challenges.HTTP01(token=(b'x' * 16))
        response, validation = chall.response_and_validation(self.account_key)

        from acme.standalone import HTTP01RequestHandler
        resource = HTTP01RequestHandler.HTTP01Resource(
            chall=chall, response=response, validation=validation)
        if add:
            self.resources.add(resource)
        return resource.response.simple_verify(
            resource.chall, 'localhost', self.account_key.public_key(),
            port=self.port)

    def test_http01_found(self):
        self.assertTrue(self._test_http01(add=True))

    def test_http01_not_found(self):
        self.assertFalse(self._test_http01(add=False))


class BaseDualNetworkedServersTest(unittest.TestCase):
    """Test for acme.standalone.BaseDualNetworkedServers."""


    class SingleProtocolServer(socketserver.TCPServer):
        """Server that only serves on a single protocol. FreeBSD has this behavior for AF_INET6."""
        def __init__(self, *args, **kwargs):
            ipv6 = kwargs.pop("ipv6", False)
            if ipv6:
                self.address_family = socket.AF_INET6
                kwargs["bind_and_activate"] = False
            else:
                self.address_family = socket.AF_INET
            socketserver.TCPServer.__init__(self, *args, **kwargs)
            if ipv6:
                # NB: On Windows, socket.IPPROTO_IPV6 constant may be missing.
                # We use the corresponding value (41) instead.
                level = getattr(socket, "IPPROTO_IPV6", 41)
                self.socket.setsockopt(level, socket.IPV6_V6ONLY, 1)
                try:
                    self.server_bind()
                    self.server_activate()
                except:
                    self.server_close()
                    raise

    @mock.patch("socket.socket.bind")
    def test_fail_to_bind(self, mock_bind):
        mock_bind.side_effect = socket.error
        from acme.standalone import BaseDualNetworkedServers
        self.assertRaises(socket.error, BaseDualNetworkedServers,
                          BaseDualNetworkedServersTest.SingleProtocolServer,
                          ('', 0),
                          socketserver.BaseRequestHandler)

    def test_ports_equal(self):
        from acme.standalone import BaseDualNetworkedServers
        servers = BaseDualNetworkedServers(
            BaseDualNetworkedServersTest.SingleProtocolServer,
            ('', 0),
            socketserver.BaseRequestHandler)
        socknames = servers.getsocknames()
        prev_port = None
        # assert ports are equal
        for sockname in socknames:
            port = sockname[1]
            if prev_port:
                self.assertEqual(prev_port, port)
            prev_port = port


class TLSSNI01DualNetworkedServersTest(unittest.TestCase):
    """Test for acme.standalone.TLSSNI01DualNetworkedServers."""


    def setUp(self):
        self.certs = {b'localhost': (
            test_util.load_pyopenssl_private_key('rsa2048_key.pem'),
            test_util.load_cert('rsa2048_cert.pem'),
        )}
        from acme.standalone import TLSSNI01DualNetworkedServers
        self.servers = TLSSNI01DualNetworkedServers(('localhost', 0), certs=self.certs)
        self.servers.serve_forever()

    def tearDown(self):
        self.servers.shutdown_and_server_close()

    def test_connect(self):
        socknames = self.servers.getsocknames()
        # connect to all addresses
        for sockname in socknames:
            host, port = sockname[:2]
            cert = crypto_util.probe_sni(
                b'localhost', host=host, port=port, timeout=1)
            self.assertEqual(jose.ComparableX509(cert),
                             jose.ComparableX509(self.certs[b'localhost'][1]))


class HTTP01DualNetworkedServersTest(unittest.TestCase):
    """Tests for acme.standalone.HTTP01DualNetworkedServers."""


    def setUp(self):
        self.account_key = jose.JWK.load(
            test_util.load_vector('rsa1024_key.pem'))
        self.resources = set() # type: Set

        from acme.standalone import HTTP01DualNetworkedServers
        self.servers = HTTP01DualNetworkedServers(('', 0), resources=self.resources)

        self.port = self.servers.getsocknames()[0][1]
        self.servers.serve_forever()

    def tearDown(self):
        self.servers.shutdown_and_server_close()

    def test_index(self):
        response = requests.get(
            'http://localhost:{0}'.format(self.port), verify=False)
        self.assertEqual(
            response.text, 'ACME client standalone challenge solver')
        self.assertTrue(response.ok)

    def test_404(self):
        response = requests.get(
            'http://localhost:{0}/foo'.format(self.port), verify=False)
        self.assertEqual(response.status_code, http_client.NOT_FOUND)

    def _test_http01(self, add):
        chall = challenges.HTTP01(token=(b'x' * 16))
        response, validation = chall.response_and_validation(self.account_key)

        from acme.standalone import HTTP01RequestHandler
        resource = HTTP01RequestHandler.HTTP01Resource(
            chall=chall, response=response, validation=validation)
        if add:
            self.resources.add(resource)
        return resource.response.simple_verify(
            resource.chall, 'localhost', self.account_key.public_key(),
            port=self.port)

    def test_http01_found(self):
        self.assertTrue(self._test_http01(add=True))

    def test_http01_not_found(self):
        self.assertFalse(self._test_http01(add=False))


class TestSimpleTLSSNI01Server(unittest.TestCase):
    """Tests for acme.standalone.simple_tls_sni_01_server."""


    def setUp(self):
        # mirror ../examples/standalone
        self.test_cwd = tempfile.mkdtemp()
        localhost_dir = os.path.join(self.test_cwd, 'localhost')
        os.makedirs(localhost_dir)
        shutil.copy(test_util.vector_path('rsa2048_cert.pem'),
                    os.path.join(localhost_dir, 'cert.pem'))
        shutil.copy(test_util.vector_path('rsa2048_key.pem'),
                    os.path.join(localhost_dir, 'key.pem'))

        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind(('', 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.port = sock.getsockname()[1]

        self.process = multiprocessing.Process(target=_simple_tls_sni_01_server_no_warnings,
                                               args=(['path', '-p', str(self.port)],))
        self.old_cwd = os.getcwd()
        os.chdir(self.test_cwd)

    def tearDown(self):
        os.chdir(self.old_cwd)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
            # Check that we didn't timeout waiting for the process to
            # terminate.
            self.assertNotEqual(self.process.exitcode, None)
        shutil.rmtree(self.test_cwd)

    @mock.patch('acme.standalone.TLSSNI01Server.handle_request')
    def test_mock(self, handle):
        _simple_tls_sni_01_server_no_warnings(cli_args=['path', '-p', str(self.port)],
                                              forever=False)
        self.assertEqual(handle.call_count, 1)

    def test_live(self):
        self.process.start()
        cert = None
        for _ in range(50):
            time.sleep(0.1)
            try:
                cert = crypto_util.probe_sni(b'localhost', b'127.0.0.1', self.port)
                break
            except errors.Error:  # pragma: no cover
                pass
        self.assertEqual(jose.ComparableX509(cert),
                         test_util.load_comparable_cert('rsa2048_cert.pem'))


def _simple_tls_sni_01_server_no_warnings(*args, **kwargs):
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', 'simple_tls.*')
        from acme.standalone import simple_tls_sni_01_server
        return simple_tls_sni_01_server(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()  # pragma: no cover
