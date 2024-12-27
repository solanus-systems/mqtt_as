import gc
import logging
import struct
from binascii import hexlify
from errno import ECONNRESET, EINPROGRESS, ENOTCONN, ETIMEDOUT
from sys import implementation, platform

from micropython import const
from mqtt_v5_properties import decode_properties, encode_properties

try:
    import asyncio
    import socket
    import time
    from time import ticks_diff, ticks_ms

    print("running in micropython")
except ImportError:
    import micropython  # for @micropython.native stub

    socket = micropython.patch_socket()
    asyncio = micropython.patch_asyncio()
    time = micropython.patch_time()
    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff
    print("running in cpython")
gc.collect()

try:
    from machine import unique_id
except ImportError:
    # micropython unix port also lacks unique_id
    print(" - unix port")

    def unique_id():
        import random

        return f'some.uid.{random.choice("abcdefg")}s{random.randint(0,9999)}'.encode(
            "ascii"
        )


gc.collect()

# TODO: set a new version
# VERSION = (0, 7, 0) 

# Legitimate errors while waiting on a socket. See asyncio __init__.py open_connection().
ESP32 = platform == "esp32"
LINUX = platform == "linux"

LINK_DOWN_ERRORS = [ENOTCONN, ECONNRESET]
BUSY_ERRORS = [EINPROGRESS, ETIMEDOUT]
if ESP32:
    # https://forum.micropython.org/viewtopic.php?f=16&t=3608&p=20942#p20942
    BUSY_ERRORS += [118, 119]  # Add in weird ESP32 errors
if LINUX:
    BUSY_ERRORS += [11]  # BlockingIOError Resource temporarily unavailable


class MsgQueue:
    def __init__(self, size):
        self._q = [0 for _ in range(max(size, 4))]
        self._size = size
        self._wi = 0
        self._ri = 0
        self._evt = asyncio.Event()
        self.discards = 0

    def put(self, *v):
        self._q[self._wi] = v
        self._evt.set()
        self._wi = (self._wi + 1) % self._size
        if self._wi == self._ri:  # Would indicate empty
            self._ri = (self._ri + 1) % self._size  # Discard a message
            self.discards += 1

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._ri == self._wi:  # Empty
            self._evt.clear()
            await self._evt.wait()
        r = self._q[self._ri]
        self._ri = (self._ri + 1) % self._size
        return r


config = {
    "client_id": hexlify(unique_id()),
    "server": None,
    "port": 0,
    "user": "",
    "password": "",
    "keepalive": 60,
    "ping_interval": 0,
    "ssl": False,
    "ssl_params": {},
    "response_time": 10,
    "clean_init": True,
    "clean": True,
    "max_repubs": 4,
    "will": None,
    "queue_len": 2,  # must define, callbacks has been removed
    "connect_props": None,
}

default_logger = logging.getLogger("mqtt_as")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


class MQTTException(Exception):
    pass


def pid_gen():
    pid = 0
    while True:
        pid = pid + 1 if pid < 65535 else 1
        yield pid


def qos_check(qos):
    if not (qos == 0 or qos == 1):
        raise ValueError("Only qos 0 and 1 are supported.")


# MQTT_base class. Handles MQTT protocol on the basis of a good connection.
class MQTT_base:
    REPUB_COUNT = 0  # TEST

    def __init__(self, config, logger=default_logger):
        self._logger = logger
        self._use_poll_fix = getattr(implementation, "_mpy", 2400) <= 2310
        # MQTT config
        self._client_id = config["client_id"]
        self._user = config["user"]
        self._password = config["password"]

        self._keepalive = config["keepalive"]
        if self._keepalive >= 65536:
            raise ValueError("invalid keepalive time")
        self._response_time = (
            config["response_time"] * 1000
        )  # Repub if no PUBACK received (ms).
        keepalive = 1000 * self._keepalive  # ms
        self._ping_interval = keepalive // 4 if keepalive else 20000

        self._max_repubs = config["max_repubs"]
        self._clean_init = config[
            "clean_init"
        ]  # clean_session state on first connection
        self._clean = config["clean"]  # clean_session state on reconnect
        will = config["will"]
        if will is None:
            self._lw_topic = False
        else:
            self._set_last_will(*will)
        self._ssl = config["ssl"]
        self._ssl_params = config["ssl_params"]
        # Callbacks and coroutines
        self.up = asyncio.Event()
        self.down = asyncio.Event()
        self.queue = MsgQueue(config["queue_len"])
        # Network
        self.port = config["port"]
        if self.port == 0:
            self.port = 8883 if self._ssl else 1883
        self.server = config["server"]
        if self.server is None:
            raise ValueError("no server specified.")
        self._addr = None  # cache the resolved DNS target
        self._sock: socket.socket = None  # type:ignore
        self._sock_connect_timeout: int = 5 * 1000  # ms

        self.new_pid = pid_gen()
        self.rcv_pids = set()  # PUBACK and SUBACK pids awaiting ACK response
        self.last_rx = ticks_ms()  # Time of last communication from broker
        self.lock = asyncio.Lock()

        self._has_connected = False
        self._in_connect = False  # connect() as need _as_read/_as_write

        self._connect_props = config["connect_props"]
        self.topic_alias_maximum = 0

    def _set_disconnect(self, rc):
        if self._has_connected:
            self._logger.warning("mq: down rc:", rc)
            self._has_connected = False
            self.down.set()

    def _set_last_will(self, topic, msg, retain=False, qos=0):
        qos_check(qos)
        if not topic:
            raise ValueError("Empty topic.")
        self._lw_topic = topic
        self._lw_msg = msg
        self._lw_qos = qos
        self._lw_retain = retain

    def _timeout(self, t):
        return ticks_diff(ticks_ms(), t) > self._response_time

    async def _as_read(self, n, sock=None):  # OSError caught by superclass
        if sock is None:
            sock = self._sock
        # Declare a byte array of size n. That space is needed anyway, better
        # to just 'allocate' it in one go instead of appending to an
        # existing object, this prevents reallocation and fragmentation.
        data = bytearray(n)
        buffer = memoryview(data)
        size = 0
        t = ticks_ms()
        while size < n:
            if not self._has_connected and not self._in_connect:
                raise OSError(-1, "Not connected on socket read")
            elif self._timeout(t):
                self._set_disconnect("rtimeout")
                raise OSError(-1, "Timeout on socket read")
            try:
                msg_size = sock.readinto(buffer[size:], n - size)
            except OSError as e:  # ESP32 issues weird 119 errors here
                msg_size = None
                if e.args[0] in LINK_DOWN_ERRORS:
                    self._set_disconnect("aread")
                    continue
                else:
                    if e.args[0] not in BUSY_ERRORS:
                        self._logger.debug("5e args: %s", e.args)
                        raise
            if msg_size == 0:  # Connection closed by host
                self._set_disconnect("rhclose")
                raise OSError(-1, "Connection closed by host")
            if msg_size is not None:  # data received
                size += msg_size
                t = ticks_ms()
                self.last_rx = ticks_ms()
            await asyncio.sleep_ms(0)
        return data

    async def _as_write(self, bytes_wr: bytes, length=0, sock=None):
        if sock is None:
            sock = self._sock

        # Wrap bytes in memoryview to avoid copying during slicing
        if not isinstance(bytes_wr, memoryview):
            bytes_wr = memoryview(bytes_wr)
        if length:
            bytes_wr = bytes_wr[:length]
        t = ticks_ms()
        while bytes_wr:
            if not self._has_connected and not self._in_connect:
                raise OSError(-1, "Not connected on socket write")
            elif self._timeout(t):
                self._set_disconnect("wtimeout")
                raise OSError(-1, "Timeout on socket write")
            try:
                n = sock.write(bytes_wr)
            except OSError as e:  # ESP32 issues weird 119 errors here
                n = 0
                if e.errno in LINK_DOWN_ERRORS:
                    self._set_disconnect("awrt")
                    continue
                else:
                    if e.args[0] not in BUSY_ERRORS:
                        self._logger.debug("6e args: %s, msg: %s", e.args, bytes_wr)
                        raise
            if n:
                t = ticks_ms()
                bytes_wr = bytes_wr[n:]
            await asyncio.sleep_ms(0)

    async def _send_str(self, s):
        await self._as_write(struct.pack("!H", len(s)))
        await self._as_write(s)

    async def _recv_len(self):
        n = 0
        sh = 0
        i = 0
        while 1:
            res = await self._as_read(1)
            i += 1
            b = res[0]
            n |= (b & 0x7F) << sh
            if not b & 0x80:
                return n, i
            sh += 7

    def resolve(self, host):
        address4 = []
        for _ in range(0, 5):
            try:
                address = socket.getaddrinfo(
                    self.server, self.port, socket.AF_INET, socket.SOCK_STREAM
                )
                for i, addr in enumerate(address):
                    self._logger.debug("DNS#%s: %s", i, addr)
                    address4.append(addr[-1])
                address_count = len(address4)
                if address_count > 0:
                    if address_count > 1:
                        self._logger.debug(f"{host} has {address_count} IPs")
                    import random

                    return random.choice(address4)
            except Exception as ex:
                if ex.errno == -3:
                    self._logger.debug("DNS failed, retrying")
                    continue
                else:
                    self._logger.error("DNS unknown err:", ex.args)
        raise OSError(-1, "DNS failed")

    def _connect_poll_fix(self):
        import select

        poller = select.poll()
        poller.register(self._sock, select.POLLIN | select.POLLOUT)

        try:
            self._sock.connect(self._addr)
        except OSError as e:
            if e.errno != EINPROGRESS:
                raise e

        self._logger.debug("- poll_fix: polling sock for connect open")
        res = poller.poll(self._sock_connect_timeout)
        # self._logger.info("c2u", res)
        poller.unregister(self._sock)
        # self._logger.info("c2ud", res)
        if not res:
            # self._logger.info("c2e", res)
            self._sock.close()
            raise OSError("Socket Connect Timeout")

    def _connect(self):
        if self._sock and self._sock.fileno() > 0:
            self._logger.debug(
                "found socket %s left open before connect, closing", self._sock
            )
            try:
                self._sock.close()
            except Exception as ex:
                self._logger.debug("error closing socket, ignored: %s", ex)
            gc.collect()

        self._logger.debug("mq: creating socket")
        self._sock = socket.socket()
        if not self._addr:
            self._addr = self.resolve(self.server)
            gc.collect()

        self._logger.debug("mq:sock connecting to: %s", self._addr)
        self._sock.setblocking(False)
        if self._use_poll_fix:
            self._connect_poll_fix()
            gc.collect()
        else:
            try:
                self._sock.connect(self._addr)
            except Exception as ex:
                raise ex
        if self._sock.fileno() < 0:
            raise OSError("Socket Connect Failed, RST?")
        self._logger.debug("sock#", self._sock.fileno())

        # Socket connected

    async def connect(self, clean):
        self._in_connect = True
        self._has_connected = False
        self._logger.debug(f"mq:Connecting to broker {self.server}:{self.port}")
        try:
            self._connect()
            self._logger.debug("mq: socket connected")
        except OSError as e:
            if e.args[0] not in BUSY_ERRORS:
                self._logger.debug("mq: socket connect err, %s", e)
                raise
        await asyncio.sleep_ms(0)
        if self._ssl:
            import ssl

            self._sock = ssl.wrap_socket(self._sock, **self._ssl_params)
        pre_msg = bytearray(b"\x10\0\0\0\0\0")
        msg = bytearray(b"\x04MQTT\x05\0\0\0")

        sz = 10 + 2 + len(self._client_id)
        msg[6] = clean << 1
        if self._user:
            sz += 2 + len(self._user)
            msg[6] |= 0xC0
            if self._password:
                sz += 2 + len(self._password)
                msg[6] |= 0x40
        if self._keepalive:
            msg[7] |= self._keepalive >> 8
            msg[8] |= self._keepalive & 0x00FF
        if self._lw_topic:
            sz += 2 + len(self._lw_topic) + 2 + len(self._lw_msg)
            msg[6] |= 0x4 | (self._lw_qos & 0x1) << 3 | (self._lw_qos & 0x2) << 3
            msg[6] |= self._lw_retain << 5

        properties = encode_properties(self._connect_props)
        sz += len(properties)

        i = 1
        while sz > 0x7F:
            pre_msg[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        pre_msg[i] = sz
        async with self.lock:
            self._logger.debug("mq: sending connect pkt.")
            await self._as_write(pre_msg, i + 2)
            await self._as_write(msg)
            await self._as_write(properties)
            await self._send_str(self._client_id)
            if self._lw_topic:
                # We don't support will properties, so we send 0x00 for properties length
                await self._as_write(b"\x00")
                await self._send_str(self._lw_topic)
                await self._send_str(self._lw_msg)
            if self._user:
                await self._send_str(self._user)
            if self._password:
                await self._send_str(self._password)

        # Await CONNACK; read causes ECONNABORTED if broker is out
        packet_type = await self._as_read(1)
        if packet_type[0] != 0x20:
            raise OSError(-1, "CONNACK not received")

        # Only read the first 2 bytes, as properties have their own length
        connack_resp = await self._as_read(2)

        # Connect ack flags
        if connack_resp[0] != 0:
            print(connack_resp)
            # raise OSError(-1, "CONNACK flags not 0")
            pass

        # Reason code
        if connack_resp[1] != 0:
            # On MQTTv5 Reason codes below 128 may need to be handled
            # differently. For now, we just raise an error. Spec is a bit weird
            # on this.
            raise OSError(-1, "CONNACK reason code 0x%x" % connack_resp[1])

        connack_props_length, _ = await self._recv_len()
        if connack_props_length > 0:
            connack_props = await self._as_read(connack_props_length)
            decoded_props = decode_properties(connack_props, connack_props_length)
            self._logger.debug("CONNACK properties: %s", decoded_props)
            self.topic_alias_maximum = decoded_props.get(0x22, 0)

        self._logger.info("mq: connected to broker.")  # Got CONNACK
        self._in_connect = False
        self._has_connected = True
        asyncio.create_task(self._handle_msg())  # Task quits on connection fail.
        asyncio.create_task(self._keep_alive())
        self.up.set()  # Connectivity is up

    # Keep broker alive MQTT spec 3.1.2.10 Keep Alive.
    # Runs until ping failure or no response in keepalive period.
    async def _keep_alive(self):
        while True:
            pings_due = ticks_diff(ticks_ms(), self.last_rx) // self._ping_interval
            if pings_due >= 4:
                self._logger.debug("kalive: broker timeout.")
                break
            await asyncio.sleep_ms(self._ping_interval)
            try:
                if self._has_connected:
                    await self._ping()
                else:
                    break
            except OSError:
                break
        self._set_disconnect("kplive")

    async def _handle_msg(self):
        while self._has_connected:
            async with self.lock:
                try:
                    await self._wait_msg()  # Immediate return if no message
                except Exception as e:
                    if e.args[0] in LINK_DOWN_ERRORS:
                        self._set_disconnect("wmsg")
                        continue
            await asyncio.sleep_ms(0)  # Let other tasks get lock

    async def _ping(self):
        async with self.lock:
            await self._as_write(b"\xc0\0")

    async def broker_up(self):  # Test broker connectivity
        if not self._has_connected:
            return False
        tlast = self.last_rx
        if ticks_diff(ticks_ms(), tlast) < 1000:
            return True
        try:
            await self._ping()
        except OSError:
            return False
        t = ticks_ms()
        while not self._timeout(t):
            await asyncio.sleep_ms(100)
            if ticks_diff(self.last_rx, tlast) > 0:  # Response received
                return True
        return False

    async def disconnect(self):
        if self._sock is not None:
            try:
                async with self.lock:
                    self._sock.write(b"\xe0\0")  # Close broker connection
                    await asyncio.sleep_ms(100)
            except OSError:
                pass
            self._close()
        self._set_disconnect("disc")

    def _close(self):
        if self._sock is not None:
            self._sock.close()

    async def _await_pid(self, pid):
        t = ticks_ms()
        while pid in self.rcv_pids:  # local copy
            if self._timeout(t) or not self._has_connected:
                break  # Must repub or bail out
            await asyncio.sleep_ms(100)
        else:
            return True  # PID received. All done.
        return False

    # qos == 1: coroutine blocks until _wait_msg gets correct PID.
    # If WiFi fails completely subclass re-publishes with new PID.
    async def publish(self, topic, msg: bytes, retain=False, qos=0, properties=None):
        pid = next(self.new_pid)
        if qos:
            self.rcv_pids.add(pid)
        async with self.lock:
            await self._publish(topic, msg, retain, qos, 0, pid, properties)
        if qos == 0:
            return

        count = 0
        while 1:  # Await PUBACK, republish on timeout
            if await self._await_pid(pid):
                return
            # No match
            if count >= self._max_repubs or not self._has_connected:
                raise OSError(-1)  # Subclass to re-publish with new PID
            async with self.lock:
                await self._publish(topic, msg, retain, qos, dup=1, pid=pid)  # Add pid
                await asyncio.sleep_ms(0)  # no_tightloop
            count += 1
            self.REPUB_COUNT += 1

    # FIXME: errors in unix mpy; could it be built with MICROPY_EMIT_NATIVE?
    # @micropython.native
    def _mk_pub_header(self, pkt, sz2, retain, qos, dup, properties):
        pkt[0] |= qos << 1 | retain | dup << 3
        sz = 2 + sz2
        if qos > 0:
            sz += 2
        if sz >= 0x200000:  # 128**3=2MB
            return -1
        i = 1
        while sz > 0x7F:
            pkt[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        pkt[i] = sz
        return i

    async def _publish(self, topic, msg: bytes, retain, qos, dup, pid, properties=None):
        pkt = bytearray(b"\x30\0\0\0")
        props = encode_properties(properties)
        topic_len = len(topic)
        msg_len = len(msg)
        props_len = len(props)
        base_len = topic_len + msg_len + props_len
        i = self._mk_pub_header(pkt, base_len, retain, qos, dup, properties)
        if i < 0:
            raise MQTTException("Strings too long.")
        try:
            await self._as_write(pkt, i + 1)
            await self._send_str(topic)
            if qos > 0:
                struct.pack_into("!H", pkt, 0, pid)
                await self._as_write(pkt, 2)
            await self._as_write(props)
            await self._as_write(msg)
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_disconnect("pub")
            raise

    # Can raise OSError if WiFi fails.
    async def subscribe(self, topic, qos, properties=None):
        pkt = bytearray(b"\x82\0\0\0")
        pid = next(self.new_pid)
        self.rcv_pids.add(pid)
        sz = 2 + 2 + len(topic) + 1
        properties = encode_properties(properties)
        sz += len(properties)
        struct.pack_into("!BH", pkt, 1, sz, pid)

        try:
            async with self.lock:
                await self._as_write(pkt)
                await self._as_write(properties)
                await self._send_str(topic)
                # Only QoS is supported other features such as:
                # (NL) No Local, (RAP) Retain As Published and Retain Handling
                # Are not supported.
                await self._as_write(qos.to_bytes(1, "little"))
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_disconnect("sub")

        if not await self._await_pid(pid):
            raise OSError(-1)

    # Can raise OSError if WiFi fails.
    async def unsubscribe(self, topic, properties=None):
        pkt = bytearray(b"\xa2\0\0\0")
        pid = next(self.new_pid)
        self.rcv_pids.add(pid)
        sz = 2 + 2 + len(topic)
        properties = encode_properties(properties)
        sz += len(properties)
        struct.pack_into("!BH", pkt, sz, pid)

        try:
            async with self.lock:
                await self._as_write(pkt)
                await self._as_write(properties)
                await self._send_str(topic)
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_disconnect("unsub")

        if not await self._await_pid(pid):
            raise OSError(-1)

    # Wait for a single incoming MQTT message and process it.
    # Subscribed messages are delivered to a callback previously
    # set by .setup() method. Other (internal) MQTT
    # messages processed internally.
    # Immediate return if no data available. Called from ._handle_msg().
    async def _wait_msg(self):  # with lock from _handle_msg()
        try:
            res = self._sock.read(1)  # Throws OSError on WiFi fail
        except OSError as e:
            if e.args[0] in BUSY_ERRORS:  # Needed by RP2
                await asyncio.sleep_ms(0)
                return
            raise
        if res is None:
            return
        if res == b"":
            raise OSError(-1, "Empty response")

        if res == b"\xd0":  # PINGRESP
            self._logger.debug("mq.rcv:pingrsp")
            await self._as_read(1)  # Update .last_rx time
            return
        op = res[0]

        if op == 0x40:  # PUBACK: save pid
            sz, _ = await self._recv_len()
            rcv_pid = await self._as_read(2)
            pid = rcv_pid[0] << 8 | rcv_pid[1]
            # For some reason even on MQTTv5 reason code is optional
            if sz != 2:
                reason_code = await self._as_read(1)
                reason_code = reason_code[0]
                if reason_code >= 0x80:
                    raise OSError(-1, "PUBACK reason code 0x%x" % reason_code)
            if sz > 3:
                puback_props_sz, _ = await self._recv_len()
                if puback_props_sz > 0:
                    puback_props = await self._as_read(puback_props_sz)
                    decoded_props = decode_properties(puback_props, puback_props_sz)
                    self._logger.debug("PUBACK properties %s", decoded_props)
            if pid in self.rcv_pids:
                self.rcv_pids.discard(pid)
            else:
                raise OSError(-1, "Invalid pid in PUBACK packet")

        if op == 0x90:  # SUBACK
            sz, _ = await self._recv_len()
            rcv_pid = await self._as_read(2)
            sz -= 2
            pid = rcv_pid[0] << 8 | rcv_pid[1]
            # Handle properties
            suback_props_sz, sz_len = await self._recv_len()
            sz -= sz_len
            sz -= suback_props_sz
            if suback_props_sz > 0:
                suback_props = await self._as_read(suback_props_sz)
                decoded_props = decode_properties(suback_props, suback_props_sz)
                self._logger.debug("SUBACK properties %s", decoded_props)

            if sz > 1:
                raise OSError(-1, "Got too many bytes")

            reason_code = await self._as_read(sz)
            reason_code = reason_code[0]
            if reason_code >= 0x80:
                raise OSError(-1, "SUBACK reason code 0x%x" % reason_code)

            if pid in self.rcv_pids:
                self.rcv_pids.discard(pid)
            else:
                raise OSError(-1, "Invalid pid in SUBACK packet")

        if op == 0xB0:  # UNSUBACK
            self._logger.debug("mq.rcv:unsubAck")
            resp = await self._as_read(3)
            pid = resp[2] | (resp[1] << 8)
            if pid in self.rcv_pids:
                self.rcv_pids.discard(pid)
            else:
                raise OSError(-1)

        if op == 0xE0:  # DISCONNECT
            sz, _ = await self._recv_len()
            reason_code = await self._as_read(1)
            reason_code = reason_code[0]

            sz -= 1
            if sz > 0:
                dis_props_sz, dis_len = await self._recv_len()
                sz -= dis_len
                disconnect_props = await self._as_read(dis_props_sz)
                decoded_props = decode_properties(disconnect_props, dis_props_sz)
                self._logger.debug("DISCONNECT properties %s", decoded_props)

            if reason_code >= 0x80:
                raise OSError(-1, "DISCONNECT reason code 0x%x" % reason_code)

        if op & 0xF0 != 0x30:
            return

        sz, _ = await self._recv_len()
        topic_len = await self._as_read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        topic = await self._as_read(topic_len)
        sz -= topic_len + 2
        if op & 6:
            pid = await self._as_read(2)
            pid = pid[0] << 8 | pid[1]
            sz -= 2

        decoded_props = None
        pub_props_sz, pub_props_sz_len = await self._recv_len()
        sz -= pub_props_sz_len
        sz -= pub_props_sz
        if pub_props_sz > 0:
            pub_props = await self._as_read(pub_props_sz)
            decoded_props = decode_properties(pub_props, pub_props_sz)

        msg = await self._as_read(sz)
        retained = op & 0x01
        self.queue.put(topic, msg, bool(retained), decoded_props)

        if op & 6 == 2:  # qos 1
            pkt = bytearray(b"\x40\x02\0\0")  # Send PUBACK
            struct.pack_into("!H", pkt, 2, pid)
            await self._as_write(pkt)
        elif op & 6 == 4:  # qos 2 not supported
            raise OSError(-1, "QoS 2 not supported")

# TODO
class MQTTClient(MQTT_base):
    pass
