#!/usr/bin/env python3
import sys, yaml, os
import flickr_api
from flickr_api.auth import AuthHandler

config_path = os.path.join(os.environ.get("HOME", os.path.expanduser("~")), ".flickr_download")
with open(config_path) as f:
    config = yaml.safe_load(f)

flickr_api.set_keys(api_key=config["api_key"], api_secret=config["api_secret"])

token_path = os.path.join(os.environ.get("HOME", os.path.expanduser("~")), ".flickr_token")
if os.path.exists(token_path):
    flickr_api.set_auth_handler(AuthHandler.load(token_path))

user = flickr_api.Person.findByUrl(sys.argv[1])
for ps in user.getPhotosets():
    photos = getattr(ps, "photos", "?")
    videos = getattr(ps, "videos", "?")
    print(f"{ps.id} - {ps.title} ({photos} photos, {videos} videos)")
