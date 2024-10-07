import hashlib
from typing import Union
import socket
import time
import threading
from datetime import datetime
import zlib
import base64
import os
import gc


class Connection(threading.Thread):
    def __init__(
        self,
        main_node,
        sock: socket.socket,
        host: str,
        port: int,
        main_port: int,
        node_key: bytes,
        role: int,
    ):
        super(Connection, self).__init__()
        self.ping = -1
        self.pinged = -1
        self.reputation = 50

        self.host = host
        self.port = port
        self.main_port = port
        self.main_node = main_node
        self.sock = sock
        self.terminate_flag = threading.Event()
        self.last_seen = None
        self.stats = {}

        self.node_key = node_key
        self.node_id = hashlib.sha256(node_key).hexdigest().encode()
        self.role = role
        self.sock.settimeout(5)
        self.chunk_size = 1024 * 1024 * 4

        # End of transmission + compression characters for the network messages.
        self.EOT_CHAR = b"HELLOCHENQUI"
        self.COMPR_CHAR = 0x02.to_bytes(16, "big")

        # self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)  # 4MB receive buffer
        # self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32 * 1024 * 1024)  # 4MB send buffer

    def compress(self, data):
        compressed = data

        try:
            compressed = base64.b64encode(zlib.compress(data, 6))
        except Exception as e:
            self.main_node.debug_print(f"compression-error: {e}")

        return compressed

    def decompress(self, data):
        decompressed = base64.b64decode(data)

        try:
            decompressed = zlib.decompress(decompressed)
        except Exception as e:
            self.main_node.debug_print(f"decompression-error: {e}")

        return decompressed

    def send(self, data: bytes, compression: bool = False):
        try:
            if compression:
                data = self.compress(data)
                if data is not None:
                    for i in range(0, len(data), self.chunk_size):
                        chunk = data[i: i + self.chunk_size]
                        self.sock.sendall(chunk + self.COMPR_CHAR)
                    self.sock.sendall(self.EOT_CHAR)

            elif len(data) < self.chunk_size:
                self.sock.sendall(data + self.EOT_CHAR)

            else:
                num_chunks = (len(data) + self.chunk_size - 1) // self.chunk_size
                data_view = memoryview(data)
                data_size = len(data)

                for chunk_number, i in enumerate(range(0, data_size, self.chunk_size), start=1):
                    self.sock.sendall(data_view[i: i + self.chunk_size])

                    # Print debug information for every chunk, or you can choose an interval.
                    if chunk_number % 100 == 0:
                        self.main_node.debug_print(f"Sent chunk {chunk_number} of {num_chunks}")

                self.sock.sendall(self.EOT_CHAR)

        except Exception as e:
            self.main_node.debug_print(f"connection send error: {e}")
            self.stop()

    def send_from_file(self, file_name: str, tag: bytes):
        try:
            # Data is a filename. Read from file in chunks.
            with open(file_name, 'rb') as file:
                self.sock.sendall(tag)

                start_time = time.time()
                # Get the total file size
                total_size = os.fstat(file.fileno()).st_size
                chunk_size = self.chunk_size
                num_chunks = (total_size + chunk_size - 1) // chunk_size

                chunk_number = 0

                while True:
                    current_position = file.tell()
                    bytes_left = total_size - current_position

                    # Optionally print or log the number of bytes left
                    if chunk_number % 10 == 0:
                        self.main_node.debug_print(f"Bytes left to send: {bytes_left}")
                        self.main_node.debug_print(f"Sent chunk {chunk_number} of {num_chunks}")

                    chunk = file.read(chunk_size)
                    if not chunk:
                        break

                    self.sock.sendall(chunk)
                    chunk_number += 1

                    gc.collect()

                self.sock.sendall(self.EOT_CHAR)
                print(time.time() - start_time)

        except Exception as e:
            self.main_node.debug_print(f"Error sending file: {e}")
            self.stop()

    def stop(self) -> None:
        self.terminate_flag.set()

    def parse_packet(self, packet) -> Union[str, dict, bytes]:
        if packet.find(self.COMPR_CHAR) == len(packet) - 1:
            packet = self.decompress(packet[0:-1])

        return packet

        # try:
        #     packet_decoded = packet.decode()
        #
        #     try:
        #         return json.loads(packet_decoded)
        #     except json.decoder.JSONDecodeError:
        #         return packet_decoded
        #
        # except UnicodeDecodeError:
        #     return packet

    def run(self):
        buffer = b""
        b_size = 0
        shmlorp = b""
        writing_threads = []

        while not self.terminate_flag.is_set():
            chunk = b""

            file_name = f"streamed_data_{self.host}_{self.port}_{self.main_node.host}_{self.main_node.port}"
            try:
                chunk = self.sock.recv(self.chunk_size)
            except socket.timeout:
                # self.main_node.debug_print("connection timeout")
                continue
            except Exception as e:
                self.terminate_flag.set()
                self.main_node.debug_print(f"unexpected error: {e}")
                break

            if chunk:
                self.last_seen = datetime.now()

                if b"MODULE" == chunk[:6]:
                    shmlorp = chunk[:70]
                    buffer += chunk[70:]
                else:
                    buffer += chunk

                eot_pos = buffer.find(self.EOT_CHAR)

                if eot_pos >= 0:
                    packet = buffer[:eot_pos]
                    try:
                        with open(file_name, "ab") as f:
                            f.write(packet)
                    except Exception as e:
                        self.main_node.debug_print(f"file writing error: {e}")
                    buffer = buffer[eot_pos + len(self.EOT_CHAR):]
                    self.main_node.handle_message(self, b"DONE STREAM" + shmlorp)
                    shmlorp = b""

                    for t in writing_threads:
                        t.join()

                    writing_threads = []

                elif len(buffer) > 20_000_000:
                    t = threading.Thread(target=self.write_to_file, args=(file_name, buffer))
                    writing_threads.append(t)
                    t.start()

                    buffer = b""

            else:
                buffer += chunk

            gc.collect()

        self.sock.settimeout(None)
        self.sock.close()

    def write_to_file(self, file_name, buffer):
        try:
            with open(file_name, "ab") as f:
                f.write(buffer)
        except Exception as e:
            self.main_node.debug_print(f"file writing error: {e}")

    """
    Connection thread between two roles that are able to send/stream data from/to
    the connected roles.

    TODO
        Send message size before to prepare accordingly.
        Switch between saving bytes to loading directly based on packet size.
    """
