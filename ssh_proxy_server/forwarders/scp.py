import logging
import os
import time
import uuid
import re

from paramiko.common import cMSG_CHANNEL_REQUEST, cMSG_CHANNEL_CLOSE, cMSG_CHANNEL_EOF
from paramiko.message import Message

from ssh_proxy_server.forwarders.base import BaseForwarder


class SCPBaseForwarder(BaseForwarder):

    def handle_traffic(self, traffic):
        return traffic

    def handle_error(self, traffic):
        return traffic

    def forward(self):

        self.server_channel.exec_command(self.session.scp_command)  # nosec

        try:
            while self.session.running:
                # redirect stdout <-> stdin und stderr <-> stderr
                if self.session.scp_channel.recv_ready():
                    buf = self.session.scp_channel.recv(self.BUF_LEN)
                    buf = self.handle_traffic(buf)
                    self.sendall(self.server_channel, buf, self.server_channel.send)
                if self.server_channel.recv_ready():
                    buf = self.server_channel.recv(self.BUF_LEN)
                    buf = self.handle_traffic(buf)
                    self.sendall(self.session.scp_channel, buf, self.session.scp_channel.send)
                if self.session.scp_channel.recv_stderr_ready():
                    buf = self.session.scp_channel.recv_stderr(self.BUF_LEN)
                    buf = self.handle_error(buf)
                    self.sendall(self.server_channel, buf, self.server_channel.send_stderr)
                if self.server_channel.recv_stderr_ready():
                    buf = self.server_channel.recv_stderr(self.BUF_LEN)
                    buf = self.handle_error(buf)
                    self.sendall(self.session.scp_channel, buf, self.session.scp_channel.send_stderr)

                if self._closed(self.session.scp_channel):
                    self.server_channel.close()
                    self.close_session(self.session.scp_channel, 0)
                    break
                if self._closed(self.server_channel):
                    self.close_session(self.session.scp_channel, 0)
                    break
                if self.server_channel.exit_status_ready():
                    status = self.server_channel.recv_exit_status()
                    self.close_session(self.session.scp_channel, status)
                    break
                if self.session.scp_channel.exit_status_ready():
                    self.session.scp_channel.recv_exit_status()
                    self.close_session(self.session.scp_channel, 0)
                    break
                time.sleep(0.1)
        except Exception:
            logging.exception('error processing scp command')
            raise

    def sendall(self, channel, data, sendfunc):
        if not data:
            return 0
        if channel.exit_status_ready():
            return 0
        sent = 0
        newsent = 0
        while sent != len(data):
            newsent = sendfunc(data[sent:])
            if newsent == 0:
                return 0
            sent += newsent
        return sent

    def close_session(self, channel, status):
        # pylint: disable=protected-access
        if channel.closed:
            return

        if not channel.exit_status_ready():
            message = Message()
            message.add_byte(cMSG_CHANNEL_REQUEST)
            message.add_int(channel.remote_chanid)
            message.add_string("exit-status")
            message.add_boolean(False)
            message.add_int(status)
            channel.transport._send_user_message(message)

        if not channel.eof_received:
            message = Message()
            message.add_byte(cMSG_CHANNEL_EOF)
            message.add_int(channel.remote_chanid)
            channel.transport._send_user_message(message)

            message = Message()
            message.add_byte(cMSG_CHANNEL_REQUEST)
            message.add_int(channel.remote_chanid)
            message.add_string('eow@openssh.com')
            message.add_boolean(False)
            channel.transport._send_user_message(message)

        message = Message()
        message.add_byte(cMSG_CHANNEL_CLOSE)
        message.add_int(channel.remote_chanid)
        channel.transport._send_user_message(message)

        channel._unlink()


class SCPForwarder(SCPBaseForwarder):

    def __init__(self, session):
        super().__init__(session)

        self.await_response = False
        self.bytes_remaining = 0
        self.bytes_to_write = 0

        self.file_command = None
        self.file_mode = None
        self.file_size = 0
        self.file_name = ''

    def handle_command(self, traffic):
        command = traffic.decode('utf-8')

        match1 = re.match(r"([CD])([0-7]{4})\s([0-9]+)\s(.*)\n", command)
        if not match1:
            match2 = re.match(r"(E)\n", command)
            if match2:
                logging.info("got command %s", command.strip())
            return traffic

        # setze Name, Dateigröße und das zu sendende Kommando
        logging.info("got command %s", command.strip())

        self.file_command = match1[1]
        self.file_mode = match1[2]
        self.bytes_remaining = self.file_size = int(match1[3])
        self.file_name = match1[4]

        # next traffic package is a respone package
        self.await_response = True
        return traffic

    def process_data(self, traffic):
        return traffic

    def process_response(self, traffic):
        return traffic

    def handle_traffic(self, traffic):
        # ignoriert das Datenpaket
        if self.await_response:
            self.await_response = False
            return self.process_response(traffic)

        if self.bytes_remaining == 0:
            return self.handle_command(traffic)

        return self.process_data(traffic)


class SCPStorageForwarder(SCPForwarder):
    """
    Kapselt das Weiterleiten bzw. Abfangen eines SCP Kommandos und der Dateien
    die damit übertragen werden.
    """
    @classmethod
    def parser_arguments(cls):
        cls.PARSER.add_argument(
            '--scp-storage',
            dest='scp_storage_dir',
            required=True,
            help='directory to store files from scp'
        )

    def __init__(self, session):
        super().__init__(session)
        self.file_id = None
        self.tmp_file = None

    def process_data(self, traffic):
        os.makedirs(self.args.scp_storage_dir, exist_ok=True)
        if not self.file_id:
            self.file_id = str(uuid.uuid4())
        output_path = os.path.join(self.args.scp_storage_dir, self.file_id)

        # notwendig, da im letzten Datenpaket ein NULL-Byte angehängt wird
        self.bytes_to_write = min(len(traffic), self.bytes_remaining)
        self.bytes_remaining -= self.bytes_to_write
        with open(output_path, 'a+b') as tmp_file:
            tmp_file.write(traffic[:self.bytes_to_write])

        # Dateiende erreicht
        if self.file_name and self.bytes_remaining == 0:
            logging.info("file %s -> %s", self.file_name, self.file_id)
            self.file_id = None
        return traffic
