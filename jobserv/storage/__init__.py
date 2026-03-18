# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from importlib import import_module

from jobserv.settings import STORAGE_BACKEND

Storage = import_module(STORAGE_BACKEND).Storage
