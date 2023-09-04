"""
ModelServiceWorker is the worker that is started by the MMS front-end.
Communication message format: binary encoding
"""

# pylint: disable=redefined-builtin

import logging
import os
import platform
import socket
import sys
import uuid
import time
import io
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding

from ts.arg_parser import ArgParser
from ts.metrics.metric_cache_yaml_impl import MetricsCacheYamlImpl
from ts.model_loader import ModelLoaderFactory
from ts.protocol.otf_message_handler import create_load_model_response, retrieve_msg

os.environ["MPLCONFIGDIR"]="/tmp/matplotlib"
MAX_FAILURE_THRESHOLD = 5
SOCKET_ACCEPT_TIMEOUT = 3000.0
DEBUG = False
BENCHMARK = os.getenv("TS_BENCHMARK")
BENCHMARK = BENCHMARK in ["True", "true", "TRUE"]


class TorchModelServiceWorker(object):
    """
    Backend worker to handle Model Server's python service code
    """

    def __init__(
        self,
        s_type=None,
        s_name=None,
        host_addr=None,
        port_num=None,
        metrics_config=None,
        frontend_ip=None,
        frontend_port=None,
        model_name=None,
        model_file=None,
        model_decryption=False,
        decryption_key=None,
        saved_on_disk=False,
        secured_dir=None,
    ):
        self.sock_type = s_type
        self.host = host_addr
        self.port = port_num
        self.frontend_ip = frontend_ip
        self.frontend_port = frontend_port
        self.model_name = model_name
        self.model_file = model_file
        self.model_decryption = model_decryption
        self.decryption_key = decryption_key
        self.saved_on_disk = saved_on_disk
        self.secured_dir = secured_dir

        if s_type == "unix":
            if s_name is None:
                raise ValueError("Wrong arguments passed. No socket name given.")
            self.sock_name, self.port = s_name, -1
            try:
                os.remove(s_name)
            except OSError as e:
                if os.path.exists(s_name):
                    raise RuntimeError(
                        "socket already in use: {}.".format(s_name)
                    ) from e

        elif s_type == "tcp":
            self.sock_name = host_addr if host_addr is not None else "127.0.0.1"
            if port_num is None:
                raise ValueError("Wrong arguments passed. No socket port given.")
            self.port = port_num
        else:
            raise ValueError("Incomplete data provided")

        logging.info("Listening on port: %s", s_name)
        socket_family = socket.AF_INET if s_type == "tcp" else socket.AF_UNIX
        self.sock = socket.socket(socket_family, socket.SOCK_STREAM)
        self.metrics_cache = MetricsCacheYamlImpl(config_file_path=metrics_config)
        if self.metrics_cache:
            self.metrics_cache.initialize_cache()
        else:
            raise RuntimeError(f"Failed to initialize metrics from file {metrics_config}")

    def load_model(self, load_model_request):
        """
        Expected command
        {
            "command" : "load", string
            "modelPath" : "/path/to/model/file", string
            "modelName" : "name", string
            "gpu" : None if CPU else gpu_id, int
            "handler" : service handler entry point if provided, string
            "envelope" : name of wrapper/unwrapper of request data if provided, string
            "batchSize" : batch size, int
            "limitMaxImagePixels": limit pillow image max_image_pixels, bool
        }

        :param load_model_request:
        :return:
        """
        try:
            #model_dir = load_model_request["modelPath"].decode("utf-8")
            model_name = load_model_request["modelName"].decode("utf-8")
            handler = (
                load_model_request["handler"].decode("utf-8")
                if load_model_request["handler"]
                else None
            )
            envelope = (
                load_model_request["envelope"].decode("utf-8")
                if "envelope" in load_model_request
                else None
            )
            envelope = envelope if envelope is not None and len(envelope) > 0 else None

            batch_size = None
            if "batchSize" in load_model_request:
                batch_size = int(load_model_request["batchSize"])
            logging.info("model_name: %s, batchSize: %d", model_name, batch_size)

            gpu = None
            if "gpu" in load_model_request:
                gpu = int(load_model_request["gpu"])

            limit_max_image_pixels = True
            if "limitMaxImagePixels" in load_model_request:
                limit_max_image_pixels = bool(load_model_request["limitMaxImagePixels"])

            with open(self.model_file, 'rb') as model_file:
                model = io.BytesIO(model_file.read())

            if self.model_decryption:
                with open(self.decryption_key, 'rb') as key_file:
                    key = key_file.read()
                model = model.getvalue()
                cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
                decryptor = cipher.decryptor()
                unpadder = padding.PKCS7(128).unpadder()
                unpadded_data = unpadder.update(decryptor.update(model) + decryptor.finalize()) + unpadder.finalize()

                model = io.BytesIO(unpadded_data)

            if self.saved_on_disk:
                if not os.path.exists(self.secured_dir):
                    os.makedirs(self.secured_dir)
                with zipfile.ZipFile(model, 'r') as model_zip:
                    model_zip.extractall(self.secured_dir)
                model_dir = self.secured_dir
            else:
                model_dir = {}
                with zipfile.ZipFile(model, 'r') as model_zip:
                    file_list = model_zip.namelist()
                    for file_name in file_list:
                        with model_zip.open(file_name) as file:
                            model_dir[file_name] = io.BytesIO(file.read())

            self.metrics_cache.model_name = model_name
            model_loader = ModelLoaderFactory.get_model_loader()
            service = model_loader.load(
                model_name,
                model_dir,
                handler,
                gpu,
                batch_size,
                envelope,
                limit_max_image_pixels,
                self.metrics_cache
            )

            logging.debug("Model %s loaded.", model_name)

            return service, "loaded model {}".format(model_name), 200
        except MemoryError:
            return None, "System out of memory", 507

    def handle_connection(self, cl_socket):
        """
        Handle socket connection.

        :param cl_socket:
        :return:
        """
        service = None
        while True:
            if BENCHMARK:
                pr.disable()
                pr.dump_stats("/tmp/tsPythonProfile.prof")
            cmd, msg = retrieve_msg(cl_socket)
            if BENCHMARK:
                pr.enable()
            if cmd == b"I":
                resp = service.predict(msg)
                cl_socket.sendall(resp)
            elif cmd == b"L":
                service, result, code = self.load_model(msg)
                resp = bytearray()
                resp += create_load_model_response(code, result)
                cl_socket.sendall(resp)
                if code != 200:
                    raise RuntimeError("{} - {}".format(code, result))
            else:
                raise ValueError("Received unknown command: {}".format(cmd))

    def run_server(self):
        """
        Run the backend worker process and listen on a socket
        :return:
        """
        if not DEBUG:
            self.sock.settimeout(SOCKET_ACCEPT_TIMEOUT)

        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if self.sock_type == "unix":
            self.sock.bind(self.sock_name)
        else:
            self.sock.bind((self.sock_name, int(self.port)))

        self.sock.listen(1)
        logging.info("[PID]%d", os.getpid())
        logging.info("Torch worker started.")
        logging.info("Python runtime: %s", platform.python_version())
        while True:
            import requests
            while True:
                try:
                    url = "http://" + self.frontend_ip + ":" + self.frontend_port + "/models/" + self.model_name + "?IP=" + self.host + "&PORT=" + self.port
                    response = requests.put(url)
                    if response.status_code < 200 or response.status_code >=300:
                        url = "https://" + self.frontend_ip + ":" + self.frontend_port + "/models/" + self.model_name + "?IP=" + self.host + "&PORT=" + self.port
                        response = requests.put(url, verify=False)
                    break
                except requests.exceptions.ConnectionError as e:
                    print("Frontend is not ready. Retry after 30s.")
                    time.sleep(30)


            (cl_socket, _) = self.sock.accept()
            # workaround error(35, 'Resource temporarily unavailable') on OSX
            cl_socket.setblocking(True)

            logging.info("Connection accepted: %s.", cl_socket.getsockname())
            self.handle_connection(cl_socket)


if __name__ == "__main__":
    # Remove ts dir from python path to avoid module name conflict.
    ts_path = os.path.dirname(os.path.realpath(__file__))
    while ts_path in sys.path:
        sys.path.remove(ts_path)

    sock_type = None
    socket_name = None

    # noinspection PyBroadException
    try:
        logging.basicConfig(stream=sys.stdout, format="%(message)s", level=logging.INFO)
        args = ArgParser.model_service_worker_args().parse_args()
        socket_name = args.sock_name
        sock_type = args.sock_type
        host = args.host
        port = args.port
        metrics_config = args.metrics_config
        frontend_ip = args.frontend_ip
        frontend_port = args.frontend_port
        model_name = args.model_name
        model_file = args.model_file
        model_decryption = args.model_decryption
        decryption_key = args.decryption_key
        saved_on_disk = args.saved_on_disk
        secured_dir = args.secured_dir


        if BENCHMARK:
            import cProfile

            pr = cProfile.Profile()
            pr.disable()
            pr.dump_stats("/tmp/tsPythonProfile.prof")

        worker = TorchModelServiceWorker(sock_type,
                                         socket_name,
                                         host,
                                         port,
                                         metrics_config,
                                         frontend_ip,
                                         frontend_port,
                                         model_name,
                                         model_file,
                                         model_decryption,
                                         decryption_key,
                                         saved_on_disk,
                                         secured_dir)
        worker.run_server()
        if BENCHMARK:
            pr.disable()
            pr.dump_stats("/tmp/tsPythonProfile.prof")

    except socket.timeout:
        logging.error(
            "Backend worker did not receive connection in: %d", SOCKET_ACCEPT_TIMEOUT
        )
    except Exception:  # pylint: disable=broad-except
        logging.error("Backend worker process died.", exc_info=True)
    finally:
        if sock_type == "unix" and os.path.exists(socket_name):
            os.remove(socket_name)

    sys.exit(1)

