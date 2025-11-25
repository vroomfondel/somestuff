import uuid
import json
import datetime
from enum import Enum

class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "repr_json"):
            return obj.repr_json()
        elif hasattr(obj, "as_string"):
            return obj.as_string()
        elif type(obj) == uuid.UUID:
            obj: uuid.UUID
            return str(obj)
        elif type(obj) == datetime.datetime:
            obj: datetime.datetime
            return obj.isoformat()  # strftime("%Y-%m-%d %H:%M:%S %Z")
        elif type(obj) == datetime.date:
            obj: datetime.date
            return obj.strftime("%Y-%m-%d")
        elif type(obj) == datetime.timedelta:
            obj: datetime.timedelta
            return str(obj)
        elif isinstance(obj, dict) or isinstance(obj, list):
            obj: str = get_pretty_dict_json_no_sort(obj)
            return obj
        else:
            return json.JSONEncoder.default(self, obj)


def print_pretty_dict_json(data, indent: int = 4):
    logger.info(json.dumps(data, indent=indent, sort_keys=True, cls=ComplexEncoder, default=str))

def get_pretty_dict_json(data, indent: int = 4) -> str:
    return json.dumps(data, indent=indent, sort_keys=True, cls=ComplexEncoder, default=str)

def get_pretty_dict_json_no_sort(data, indent: int = 4) -> str:
    return json.dumps(data, indent=indent, sort_keys=False, cls=ComplexEncoder, default=str)