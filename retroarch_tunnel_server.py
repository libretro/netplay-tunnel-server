#  RetroArch - A frontend for libretro.
#  Copyright (C) 2021-2022 - Libretro team
#
#  RetroArch is free software: you can redistribute it and/or modify it under the terms
#  of the GNU General Public License as published by the Free Software Found-
#  ation, either version 3 of the License, or (at your option) any later version.
#
#  RetroArch is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
#  without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
#  PURPOSE.  See the GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License along with RetroArch.
#  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

from typing import (
    Optional,
    Union,
    ClassVar,
    Tuple,
    Dict,
    cast
)

import sys
import os
import enum
import socket
import ipaddress
import asyncio
import configparser

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

class Error(Exception):
    pass

@enum.unique
class LogLevel(enum.IntEnum):
    NONE  = 0
    ERROR = 1
    WARN  = 2
    INFO  = 3

class Logger:
    __slots__ = ("__path", "__level", "__lock")

    __path:  Path
    __level: LogLevel

    __lock: asyncio.Lock

    def __init__(self, path: Path, level: LogLevel = LogLevel.NONE):
        self.__path  = path
        self.__level = level

        self.__lock = asyncio.Lock()

    async def __call__(self, level: LogLevel, message: str) -> None:
        if level is LogLevel.NONE or not message:
            return # Should this be silent?

        if self.__level >= level:
            now: datetime = datetime.now()

            async with self.__lock:
                try:
                    with self.__path.open("a", encoding = "UTF-8") as log:
                        log.write(f"({now:%d}/{now:%m}/{now:%Y} {now:%H}:{now:%M}:{now:%S}) [{level.name}] {message}\n")
                except OSError:
                    pass # Should this be silent?

class ConfigError(Error):
    pass

class Config:
    __slots__ = (
        "__server_port",
        "__server_timeout",
        "__session_max",
        "__session_clients",
        "__log_path",
        "__log_level"
    )

    __server_port:    int
    __server_timeout: float

    __session_max:     int
    __session_clients: int

    __log_path:  Path
    __log_level: LogLevel

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            path = Path(sys.argv[0]).with_suffix(".ini")

        ini: configparser.ConfigParser = configparser.ConfigParser(
            delimiters = '=',
            comment_prefixes = ';',
            interpolation = None
        )

        try:
            if not ini.read(path):
                raise ConfigError("Configuration not found.")

            try:
                server: configparser.SectionProxy = ini["Server"]
            except KeyError:
                raise ConfigError("Server section not found.")
            else:
                try:
                    self.__server_port = int(server["Port"])

                    if self.__server_port not in range(1, 65536):
                        raise ValueError
                except KeyError:
                    raise ConfigError("Server port not found.")
                except ValueError:
                    raise ConfigError("Invalid server port.")

                try:
                    self.__server_timeout = float(server["Timeout"])

                    if self.__server_timeout <= 0:
                        raise ValueError
                except KeyError:
                    raise ConfigError("Server timeout not found.")
                except ValueError:
                    raise ConfigError("Invalid server timeout.")

            try:
                session: configparser.SectionProxy = ini["Session"]
            except KeyError:
                raise ConfigError("Session section not found.")
            else:
                try:
                    self.__session_max = int(session["Max"])

                    if self.__session_max < 0:
                        raise ValueError
                except KeyError:
                    raise ConfigError("Session max not found.")
                except ValueError:
                    raise ConfigError("Invalid session max.")

                try:
                    self.__session_clients = int(session["Clients"])

                    if self.__session_clients < 0:
                        raise ValueError
                except KeyError:
                    raise ConfigError("Session clients not found.")
                except ValueError:
                    raise ConfigError("Invalid session clients.")

            try:
                log: configparser.SectionProxy = ini["Log"]
            except KeyError:
                raise ConfigError("Log section not found.")
            else:
                try:
                    self.__log_path: Path = Path(log["Path"])
                except KeyError:
                    raise ConfigError("Log path not found.")
                else:
                    if self.__log_path.is_dir():
                        raise ConfigError("Invalid log path.")

                try:
                    log_level: str = log["Level"].upper()
                except KeyError:
                    raise ConfigError("Log level not found.")
                else:
                    try:
                        self.__log_level = LogLevel[log_level]
                    except KeyError:
                        raise ConfigError("Invalid log level.")
        except configparser.Error:
            raise ConfigError("Invalid configuration.")

    @property
    def port(self)                    -> int:      return self.__server_port
    @property
    def timeout(self)                 -> float:    return self.__server_timeout
    @property
    def max_sessions(self)            -> int:      return self.__session_max
    @property
    def max_clients_per_session(self) -> int:      return self.__session_clients
    @property
    def log_path(self)                -> Path:     return self.__log_path
    @property
    def log_level(self)               -> LogLevel: return self.__log_level

class TunnelError(Error):
    pass

class TunnelMagic:
    SESSION: ClassVar[bytes] = b'RATS'
    LINK:    ClassVar[bytes] = b'RATL'
    ADDRESS: ClassVar[bytes] = b'RATA'
    PING:    ClassVar[bytes] = b'RATP'

    @staticmethod
    def size()        -> int: return 4
    @staticmethod
    def unique_size() -> int: return 12

class Tunnel(ABC):
    __slots__ = ("_config", "_logger", "_lock")

    _config: Config
    _logger: Logger

    _lock: asyncio.Lock

    def __init__(self, config: Config, logger: Logger):
        self._config = config
        self._logger = logger

        self._lock = asyncio.Lock()

    async def log_info(self, message: str)  -> None:
        await self._logger(LogLevel.INFO, message)

    async def log_warn(self, message: str)  -> None:
        await self._logger(LogLevel.WARN, message)

    async def log_error(self, message: str) -> None:
        await self._logger(LogLevel.ERROR, message)

    @abstractmethod
    async def __call__(self) -> None:
        pass

class TunnelClient(Tunnel):
    INVALID_UNIQUE: ClassVar[bytes] = bytes(TunnelMagic.unique_size())

    __slots__ = ("_reader", "_writer", "_unique")

    _reader: asyncio.StreamReader
    _writer: asyncio.StreamWriter

    _unique: bytes

    def __init__(self, config: Config, logger: Logger, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        super().__init__(config, logger)

        self._reader = reader
        self._writer = writer

        self._unique = self.INVALID_UNIQUE

    def __del__(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass

    @property
    def address(self) -> str:
        return cast(str, self._writer.get_extra_info("peername")[0])

    @property
    def port(self) -> int:
        return cast(int, self._writer.get_extra_info("peername")[1])

    async def unique(self) -> bytes:
        async with self._lock:
            return self._unique

    async def set_unique(self, unique: bytes) -> None:
        async with self._lock:
            self._unique = unique

class SessionOwner(TunnelClient):
    __slots__ = ("__connected", "__ready")

    __connected: Dict[bytes, TunnelClient]

    __ready: bool

    def __init__(self, config: Config, logger: Logger, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        super().__init__(config, logger, reader, writer)

        self.__connected = {self.INVALID_UNIQUE: self} # Host counts as one.

        self.__ready = False

    def __bool__(self) -> bool:
        return self.__ready

    async def __send_address(self, unique: bytes) -> bool:
        # If unique is INVALID_UNIQUE, send the address of the session owner.
        async with self._lock:
            raw_addr: bytes

            try:
                addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address] = ipaddress.ip_address(self.__connected[unique].address)
            except (KeyError, ValueError):
                # If the requested client couldn't be found, send an invalid reply.
                unique   = self.INVALID_UNIQUE
                raw_addr = ipaddress.ip_address("::").packed
            else:
                if addr.version == 4:
                    # For IPv4, we need to add a prefix.
                    raw_addr = bytes(10) + bytes((0xff,0xff)) + addr.packed
                else:
                    raw_addr = addr.packed
            finally:
                try:
                    self._writer.write(TunnelMagic.ADDRESS + unique + raw_addr)
                    await self._writer.drain()
                except Exception:
                    return False
                else:
                    return True

    async def request_link(self, client: SessionUserClient) -> bool:
        async with self._lock:
            if not self.__ready:
                return False

            if self._reader.at_eof():
                return False

            if self._writer.is_closing():
                return False

            if self._config.max_clients_per_session and len(self.__connected) >= self._config.max_clients_per_session:
                return False

            client_unique: bytes = await client.unique()

            if client_unique in self.__connected:
                return False

            # Tell the host to establish a new link connection.
            try:
                self._writer.write(TunnelMagic.LINK + client_unique)
                await self._writer.drain()
            except Exception:
                return False
            else:
                self.__connected[client_unique] = client

        return True

    async def request_unlink(self, client: SessionUserClient) -> bool:
        async with self._lock:
            if not self.__ready:
                return False

            client_unique: bytes = await client.unique()

            # Don't remove the session owner.
            if client_unique == self.INVALID_UNIQUE:
                return False

            try:
                del self.__connected[client_unique]
            except KeyError:
                return False

        return True

    async def __call__(self) -> None:
        try:
            # The first thing is sending the host his session id.
            async with self._lock:
                self._writer.write(TunnelMagic.SESSION + self._unique)
                await self._writer.drain()

                # We are ready to receive connection requests.
                self.__ready = True

            data: bytes
            pings: int = 0

            while not self._reader.at_eof():
                try:
                    # We want to request a ping every minute.
                    data = await asyncio.wait_for(self._reader.readexactly(TunnelMagic.size()), 60)
                except asyncio.TimeoutError:
                    # Attempt to ping the host a total of 3 times before timeouting.
                    if pings < 3:
                        async with self._lock:
                            self._writer.write(TunnelMagic.PING)
                            await self._writer.drain()

                        pings += 1
                    else:
                        await self.log_error(f"Tunnel session timeout for: {self.address}|{self.port}")
                        break
                else:
                    if not data:
                        break

                    if data == TunnelMagic.ADDRESS:
                        # Do not wait for more than 30 seconds for the unique id.
                        data = await asyncio.wait_for(self._reader.readexactly(TunnelMagic.unique_size()), 30)

                        if not data:
                            break

                        if not await self.__send_address(data):
                            await self.log_error(f"Failed to send requested address for: {self.address}|{self.port}")
                            break
                    elif data == TunnelMagic.PING:
                        pings = 0
                    else:
                        await self.log_error(f"Tunnel session received unknown data for: {self.address}|{self.port}")
                        break
        except Exception:
            pass

        async with self._lock:
            self.__ready = False

        await self.log_info(f"Tunnel session closed for: {self.address}|{self.port}")

class SessionUser(TunnelClient):
    __slots__ = ("__owner", "__link", "__linked")

    __owner: Optional[SessionOwner]

    __link:   Optional[SessionUser]
    __linked: asyncio.Event

    def __init__(self, config: Config, logger: Logger, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        super().__init__(config, logger, reader, writer)

        self.__owner = None

        self.__link   = None
        self.__linked = asyncio.Event()

    def __bool__(self) -> bool:
        return self.__linked.is_set()

    async def __forward(self, data: bytes) -> bool:
        # Forward data from one peer to another through our tunnel.
        try:
            async with self._lock:
                self._writer.write(data)
                await self._writer.drain()
        except Exception:
            return False
        else:
            return True

    async def owner(self) -> Optional[SessionOwner]:
        async with self._lock:
            return self.__owner

    async def set_owner(self, owner: Optional[SessionOwner]) -> None:
        async with self._lock:
            self.__owner = owner

    async def try_link(self, link: SessionUser) -> bool:
        owner: Optional[SessionOwner] = await self.owner()

        if owner is None:
            return False

        async with owner._lock, self._lock, link._lock:
            if self.__link is not None or link.__link is not None:
                return False

            if self.__linked.is_set() or link.__linked.is_set():
                return False

            if self._reader.at_eof() or link._reader.at_eof():
                return False

            if self._writer.is_closing() or link._writer.is_closing():
                return False

            self.__link = link
            link.__link = self

            self.__linked.set()
            link.__linked.set()

        return True

    async def try_unlink(self) -> bool:
        owner: Optional[SessionOwner] = await self.owner()

        if owner is None:
            return False

        async with owner._lock, self._lock:
            link: Optional[SessionUser] = self.__link

            if link is None:
                return False

            async with link._lock:
                self.__link = None
                link.__link = None

        return True

    async def __call__(self) -> None:
        timeout: float = self._config.timeout

        try:
            # Wait until we are linked to a connection or a timeout occurs.
            await asyncio.wait_for(self.__linked.wait(), timeout)
        except asyncio.TimeoutError:
            await self.log_error(f"Timeout while awaiting link for: {self.address}|{self.port}")
        else:
            # The tunnel is ready.
            try:
                data: bytes
                link: Optional[SessionUser]

                # Forward data until the connection is closed or until a timeout occurs.
                while not self._reader.at_eof():
                    try:
                        data = await asyncio.wait_for(self._reader.read(8192), timeout)
                    except asyncio.TimeoutError:
                        await self.log_error(f"Tunnel link timeout for: {self.address}|{self.port}")
                        break

                    if not data:
                        break

                    async with self._lock:
                        link = self.__link

                    # Check if our link is still around.
                    if link is None:
                        break

                    if not await link.__forward(data):
                        await self.log_error(f"Failed to forward data from: {self.address}|{self.port}")
                        break
            except Exception:
                pass

        await self.log_info(f"Tunnel closed for: {self.address}|{self.port}")

class SessionUserClient(SessionUser):
    __slots__ = ()

class SessionUserHost(SessionUser):
    __slots__ = ()

class TunnelServer(Tunnel):
    __slots__ = ("__clients", "__sessions")

    __clients:  Dict[bytes, TunnelClient]
    __sessions: int

    def __init__(self, config: Config, logger: Logger):
        super().__init__(config, logger)

        self.__clients  = {}
        self.__sessions = 0

    async def __request_session(self) -> bool:
        if self._config.max_sessions:
            async with self._lock:
                if self.__sessions >= self._config.max_sessions:
                    return False

                self.__sessions += 1

        return True

    async def __free_session(self) -> bool:
        if self._config.max_sessions:
            async with self._lock:
                if self.__sessions <= 0:
                    return False

                self.__sessions -= 1

        return True

    async def __add_client(self, client: TunnelClient) -> None:
        # Generate a new unique id for this client.
        async with self._lock:
            unique: bytes

            while True:
                unique = os.urandom(TunnelMagic.unique_size())

                if unique == client.INVALID_UNIQUE:
                    continue

                if unique not in self.__clients:
                    break

            await client.set_unique(unique)
            self.__clients[unique] = client

    async def __remove_client(self, client: TunnelClient) -> None:
        async with self._lock:
            try:
                del self.__clients[await client.unique()]
            except KeyError:
                await self.log_warn(f"Failed to remove client: {client.address}|{client.port}")

    async def __session_link_request(self, user: SessionUserClient, session_id: bytes) -> Optional[SessionOwner]:
        unique: bytes = await user.unique()

        if session_id == unique:
            return await self.log_error(f"Invalid session link request from: {user.address}|{user.port}")

        try:
            async with self._lock:
                owner: TunnelClient = self.__clients[session_id]
        except KeyError:
            return await self.log_error(f"Failed to find session for: {user.address}|{user.port}")
        else:
            if not isinstance(owner, SessionOwner):
                return await self.log_error(f"Invalid session id from: {user.address}|{user.port}")

            await user.set_owner(owner)

            if not await owner.request_link(user):
                return await self.log_error(f"Failed to establish tunnel link for: {user.address}|{user.port}")

        return owner

    async def __session_link(self, user: SessionUserHost, peer_id: bytes) -> Optional[SessionUserClient]:
        unique: bytes = await user.unique()

        if peer_id == unique:
            return await self.log_error(f"Invalid peer link request from: {user.address}|{user.port}")

        try:
            async with self._lock:
                peer: TunnelClient = self.__clients[peer_id]
        except KeyError:
            return await self.log_error(f"Failed to find peer for: {user.address}|{user.port}")
        else:
            if not isinstance(peer, SessionUserClient):
                return await self.log_error(f"Invalid peer id from: {user.address}|{user.port}")

            owner: Optional[SessionOwner] = await peer.owner()

            if owner is None:
                return await self.log_error(f"Invalid peer session owner for: {user.address}|{user.port}")

            await user.set_owner(owner)

            if not await peer.try_link(user):
                return await self.log_error(f"Failed to establish tunnel link for: {user.address}|{user.port}")

        return peer

    async def __handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock_info: Tuple[Union[str, int], ...] = writer.get_extra_info("peername")
            addr:      str                         = cast(str, sock_info[0])
            port:      int                         = cast(int, sock_info[1])

            await self.log_info(f"Received connection from: {addr}|{port}")

            try:
                writer.get_extra_info("socket").setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
            except Exception:
                await self.log_warn(f"Failed to set TCP_NODELAY for: {addr}|{port}")

            try:
                # Do not wait for more than 30 seconds for the magic.
                magic: bytes = await asyncio.wait_for(reader.readexactly(TunnelMagic.size()), 30)
            except Exception:
                return await self.log_error(f"Failed to receive tunnel magic from: {addr}|{port}")

            if magic == b'RANP':
                # Client does not support tunnels.
                # Send a fake header to let it know it's outdated.
                writer.write(b'RANP' + bytes(20))
                await writer.drain()

                return await self.log_error(f"Unsupported client from: {addr}|{port}")
            elif magic in (TunnelMagic.SESSION, TunnelMagic.LINK):
                try:
                    # Do not wait for more than 30 seconds for the unique id.
                    unique: bytes = await asyncio.wait_for(reader.readexactly(TunnelMagic.unique_size()), 30)
                except Exception:
                    return await self.log_error(f"Failed to receive tunnel unique id from: {addr}|{port}")

                client: TunnelClient
                owner:  Optional[SessionOwner]
                peer:   Optional[SessionUserClient]

                if magic == TunnelMagic.SESSION:
                    if unique == TunnelClient.INVALID_UNIQUE:
                        # Client requested a new session.
                        if await self.__request_session():
                            client = SessionOwner(self._config, self._logger, reader, writer)
                            await self.__add_client(client)

                            await self.log_info(f"Tunnel session created for: {addr}|{port}")
                        else:
                            return await self.log_error(f"Refused to create tunnel session for: {addr}|{port}")
                    else:
                        # Client trying to link to an existing session.
                        client = SessionUserClient(self._config, self._logger, reader, writer)
                        await self.__add_client(client)
                        owner = await self.__session_link_request(client, unique)

                        if owner is not None:
                            await self.log_info(f"Pending tunnel linking for: {addr}|{port}")
                        else:
                            await self.__remove_client(client)
                            return await self.log_error(f"Tunnel linking failed for: {addr}|{port}")
                else:
                    # Session host client trying to link to an user client.
                    client = SessionUserHost(self._config, self._logger, reader, writer)
                    await self.__add_client(client)
                    peer = await self.__session_link(client, unique)

                    if peer is not None:
                        await self.log_info(
                            f"Tunnel linking completed for: {peer.address}|{peer.port} <-> {addr}|{port}"
                        )
                    else:
                        await self.__remove_client(client)
                        return await self.log_error(f"Tunnel linking failed for: {addr}|{port}")

                await client()

                await self.__remove_client(client)

                if isinstance(client, SessionOwner):
                    await self.__free_session()
                else:
                    # Make sure to tell our link we are done for.
                    await cast(SessionUser, client).try_unlink()

                    if isinstance(client, SessionUserClient):
                        await cast(SessionOwner, owner).request_unlink(client)
            else:
                return await self.log_error(f"Unknown tunnel magic from: {addr}|{port}")
        finally:
            # Close the connection once we are done.
            try:
                writer.write_eof()
            except Exception:
                pass

            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

            await self.log_info(f"Connection closed for: {addr}|{port}")

    async def __call__(self) -> None:
        server: asyncio.AbstractServer

        try:
            if sys.platform == "win32":
                server = await asyncio.start_server(self.__handle_client, port = self._config.port)
            else:
                server = await asyncio.start_server(self.__handle_client, port = self._config.port, reuse_port = True)
        except Exception:
            raise TunnelError("Failed to create tunnel server.")
        else:
            if not server.sockets:
                raise TunnelError("Failed to create tunnel server socket(s).")

        async with server:
            sock_info: Tuple[Union[str, int], ...]
            address:   str

            for sock in server.sockets:
                sock_info = sock.getsockname()
                address   = f"{sock_info[0]}|{sock_info[1]}"

                print(f"[*] Tunnel server listening on: {address}")
                await self.log_info(f"Tunnel server listening on: {address}")

            await server.serve_forever()

async def main() -> None:
    config: Config       = Config(Path(sys.argv[1].strip()) if len(sys.argv) > 1 else None)
    logger: Logger       = Logger(config.log_path, config.log_level)
    server: TunnelServer = TunnelServer(config, logger)

    await server()

if __name__ == "__main__":
    asyncio.run(main())
