from importlib import import_module

from jobserv.settings import STATS_CLIENT_MODULE

module, class_name = STATS_CLIENT_MODULE.split(":")
module = import_module(module)
StatsClient = getattr(module, class_name)
