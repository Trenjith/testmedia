import os
import sys
import re
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable
from werkzeug.wsgi import pop_path_info, peek_path_info, get_path_info
from werkzeug.exceptions import NotFound, BadRequest
import pymongo
from pymongo.errors import PyMongoError
import flask
import dill

from dataviz.serializer import dashapp_deserializer, get_attr_from_serialized_dashapp
from .api_app import create_app as create_api_app
from .config import Config, build_dashapp_server_configs
from .utils import get_logger


ALLOWED_APPNAME_PATTERN = os.getenv('ALLOWED_APPNAME_PATTERN', '^[a-zA-Z0-9_-]+$')
name_pattern = re.compile(ALLOWED_APPNAME_PATTERN)


class Dispatcher:
    """Dispatch a Dash application by a path on the URL.

    :param mongo_collection: pymMogoongo collection used to retrieve
        viz applications.
    :type mongo_collection: pymongo.collection.Collection
    """

    RETENTION_PERIOD_MIN = 60 # mins

    def __init__(self, mongo_collection: pymongo.collection.Collection):
        self.mongo_collection = mongo_collection
        self.default_app = NotFound()
        self.instances = {}
        self.api = create_api_app(mongo_collection, Config)

        # time before refresh (fetching again from db) a dapp
        self.retention_period = timedelta(
            minutes=int(os.getenv('RETENTION_PERIOD_MIN', self.RETENTION_PERIOD_MIN)))

        self.logger = get_logger()

    def __call__(self, environ, start_response):
        app = self.get_application(environ)

        if app is not None:
            pop_path_info(environ)

        else:
            app = self.default_app

        return app(environ, start_response)

    def get_application(self, environ) -> flask.Flask:
        """Retrieve a WSGI application.

        :param name: application's name (uid)
        :type name: str
        :return: WSGI application to dispatch
        :rtype: flask.Flask
        """
        name = peek_path_info(environ)

        if not name:
            return None

        elif name == 'api':
            self.logger.info(f'Get special dapp "{name}"')
            return self.api

        if not re.match(name_pattern, name):
            self.logger.info(f'"{name_pattern}" not in the allowed name pattern')
            return BadRequest()

        instance = self.instances.get(name)

        # if the retention period is expired, delete the app
        if (instance and
            Dispatcher.__is_main_page(environ) and
            (datetime.utcnow() - instance['added']) > self.retention_period
        ):
            self.logger.info(f'Dapp "{name}" has expired')
            instance = None
            del self.instances[name]

        if instance is None:
            app = self.__get_app_from_db(name)

            if app:
                instance = {
                    'app': app,
                    'added': datetime.utcnow()
                }

                self.instances[name] = instance

                self.logger.info(f'Got dapp "{name}" from db')
        else:
            app = instance['app']

        return app

    def __get_app_from_db(self, viz_uid: str) -> Optional[flask.Flask]:
        try:
            res = self.mongo_collection.find_one({'uid': viz_uid})

        except PyMongoError as err:
            self.logger.warning(err)

        if (not res) or ('dashapp' not in res):
            return None

        # Create configs to run the app with the dispatcher
        server_configs = build_dashapp_server_configs(viz_uid)

        # Build the app back
        app = dashapp_deserializer(res['dashapp'], **server_configs)

        return app.server

    def __get_app_layout_from_db(self, viz_uid: str):
        try:
            res = self.mongo_collection.find_one({'uid': viz_uid})

        except PyMongoError as err:
            self.logger.warning(err)

        if (not res) or ('dashapp' not in res):
            return None

        # don't deserialize the full app, just decode it to get the layout
        layout = get_attr_from_serialized_dashapp(res['dashapp'], 'layout')
        return layout

    @staticmethod
    def __is_main_page(environ: dict) -> bool:
        path = get_path_info(environ)
        splitted = path.rsplit('/', 2)

        return len(splitted) == 2 or (len(splitted) == 3 and splitted[-1] == '')
