import pytest

# @pytest.fixture()
# def gapp():  # type: ignore
#     def efun() -> Response:
#         raise Exception("intentionally thrown")
#
#     app = create_app()
#
#     app.add_url_rule("/exception", "exception", efun)
#
#     app.config.update(
#         {
#             "TESTING": True,
#         }
#     )
#
#     yield app
#
#
# @pytest.fixture()
# def flask_client(gapp: Flask) -> FlaskClient:
#     return gapp.test_client()
