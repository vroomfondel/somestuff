import uuid
import json
import datetime
from enum import Enum
from typing import Any


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj: Any):
        if hasattr(obj, "repr_json"):
            return obj.repr_json()
        elif hasattr(obj, "as_string"):
            return obj.as_string()
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        elif isinstance(obj, datetime.datetime):
            return obj.isoformat()  # strftime("%Y-%m-%d %H:%M:%S %Z")
        elif isinstance(obj, datetime.date):
            return obj.strftime("%Y-%m-%d")
        elif isinstance(obj, datetime.timedelta):
            return str(obj)
        elif isinstance(obj, dict) or isinstance(obj, list):
            robj: str = get_pretty_dict_json_no_sort(obj)
            return robj
        else:
            return json.JSONEncoder.default(self, obj)


def print_pretty_dict_json(data, indent: int = 4):
    from loguru import logger
    logger.info(json.dumps(data, indent=indent, sort_keys=True, cls=ComplexEncoder, default=str))

def get_pretty_dict_json(data, indent: int = 4) -> str:
    return json.dumps(data, indent=indent, sort_keys=True, cls=ComplexEncoder, default=str)


def get_pretty_dict_json_no_sort(data, indent: int = 4) -> str:
    return json.dumps(data, indent=indent, sort_keys=False, cls=ComplexEncoder, default=str)
