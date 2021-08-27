# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>
from math import ceil

from flask import jsonify, request


def _status_str(status_code):
    if status_code >= 200 and status_code < 300:
        return "success"
    if status_code >= 400 and status_code < 400:
        return "fail"
    return "error"


def jsendify(data, status_code=200):
    # https://labs.omniti.com/labs/jsend
    rv = {"status": _status_str(status_code)}
    if type(data) == str:
        rv["message"] = data
    else:
        rv["data"] = data
    resp = jsonify(rv)
    resp.status_code = status_code
    return resp


class ApiError(Exception):
    def __init__(self, status_code, data):
        super(ApiError, self).__init__()
        self.resp = jsendify(data, status_code)

    def __str__(self):
        return self.resp.data.decode()


def get_or_404(query):
    rv = query.first()
    if rv is None:
        raise ApiError(404, "Object does not exist: " + request.path)
    return rv


def paginate_custom(item_type, query, cb_func):
    try:
        limit = int(request.args.get("limit", "25"))
    except ValueError:
        raise ApiError(400, 'Invalid pagination. "limit" must be numeric')
    try:
        page = int(request.args.get("page", "0"))
    except ValueError:
        raise ApiError(400, 'Invalid pagination. "page" must be numeric')
    total = query.count()
    offset = page * limit
    pages = ceil(total / limit)
    next_page = page + 1

    items = query.limit(limit).offset(offset)
    data = {
        "limit": limit,
        "page": page,
        "pages": pages,
        "total": total,
        item_type: [cb_func(x) for x in items],
    }
    if next_page < pages:
        url = request.host_url
        if url[-1] == "/":
            url = url[:-1]
        url += request.path
        data["next"] = f"{url}?page={next_page}&limit={limit}"

    return jsendify(data)


def paginate(item_type, query):
    return paginate_custom(item_type, query, lambda x: x.as_json(detailed=False))
