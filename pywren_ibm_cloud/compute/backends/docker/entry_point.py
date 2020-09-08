import sys
import os
import uuid
import flask
import logging
import pkgutil
import multiprocessing
import time
import threading
import importlib

from pywren_ibm_cloud.version import __version__
from pywren_ibm_cloud.function import function_invoker
from pywren_ibm_cloud.config import DOCKER_FOLDER
from pywren_ibm_cloud.config import extract_compute_config
from pywren_ibm_cloud.compute.utils import get_remote_client

log_file = os.path.join(DOCKER_FOLDER, 'proxy.log')
logging.basicConfig(filename=log_file, level=logging.DEBUG)
logger = logging.getLogger('__main__')


proxy = flask.Flask(__name__)

last_usage_time = time.time()
keeper = None


def budget_keeper(client):
    global last_usage_time

    logger.info("BudgetKeeper started")
    while True:
        time_since_last_usage = time.time() - last_usage_time
        time_to_dismantle = client.dismantle_timeout - time_since_last_usage
        logger.info("Time to dismantle: {}".format(time_to_dismantle))
        if time_to_dismantle < 0:
            # unset 'PYWREN_FUNCTION' environment variable that prevents token manager generate new token
            del os.environ['PYWREN_FUNCTION']
            logger.info("Dismantling setup")
            client.dismantle()

        time.sleep(5)

def _init_keeper(config):
    global keeper
    compute_config = extract_compute_config(config)
    client = get_remote_client(compute_config)
    keeper = threading.Thread(target=budget_keeper, args=(client,))
    keeper.start()

@proxy.route('/', methods=['POST'])
def run():
    def error():
        response = flask.jsonify({'error': 'The action did not receive a dictionary as an argument.'})
        response.status_code = 404
        return complete(response)

    sys.stdout = open(log_file, 'w')

    global last_usage_time
    global keeper

    last_usage_time = time.time()

    message = flask.request.get_json(force=True, silent=True)
    if message and not isinstance(message, dict):
        return error()

    act_id = str(uuid.uuid4()).replace('-', '')[:12]
    os.environ['__PW_ACTIVATION_ID'] = act_id

    if 'remote_invoker' in message:
        try:
            # init keeper only when auto_dismantle: True and remote_client configuration provided
            auto_dismantle = message['config']['pywren'].get('auto_dismantle', True)
            if auto_dismantle and 'remote_client' in message['config']['pywren'] and not keeper:
                _init_keeper(message['config'])

            # remove 'remote_client' configuration
            message['config']['pywren'].pop('remote_client', None)

            logger.info("PyWren v{} - Starting Docker invoker".format(__version__))
            message['config']['pywren']['remote_invoker'] = False
            message['config']['pywren']['compute_backend'] = 'localhost'

            if 'localhost' not in message['config']:
                message['config']['localhost'] = {}

            if message['config']['pywren']['workers'] is None:
                total_cpus = multiprocessing.cpu_count()
                message['config']['pywren']['workers'] = total_cpus
                message['config']['localhost']['workers'] = total_cpus
            else:
                message['config']['localhost']['workers'] = message['config']['pywren']['workers']

            message['invokers'] = 0
            message['log_level'] = None

            function_invoker(message)
        except Exception as e:
            logger.info(e)

    response = flask.jsonify({"activationId": act_id})
    response.status_code = 202

    return complete(response)


@proxy.route('/preinstalls', methods=['GET', 'POST'])
def preinstalls_task():
    logger.info("Extracting preinstalled Python modules...")

    runtime_meta = dict()
    mods = list(pkgutil.iter_modules())
    runtime_meta['preinstalls'] = [entry for entry in sorted([[mod, is_pkg] for _, mod, is_pkg in mods])]
    python_version = sys.version_info
    runtime_meta['python_ver'] = str(python_version[0])+"."+str(python_version[1])
    response = flask.jsonify(runtime_meta)
    response.status_code = 200
    logger.info("Done!")

    return complete(response)


def complete(response):
    # Add sentinel to stdout/stderr
    sys.stdout.write('%s\n' % 'XXX_THE_END_OF_AN_ACTIVATION_XXX')
    sys.stdout.flush()

    return response


def main():
    port = int(os.getenv('PORT', 8080))
    proxy.run(debug=True, host='0.0.0.0', port=port)


if __name__ == '__main__':
    main()
