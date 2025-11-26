import datetime
import json
import uuid
from enum import StrEnum
from io import StringIO
from pathlib import Path


from google.genai.client import DebugConfig

import google.genai
from google.genai.types import GenerateContentResponse

from pydantic import BaseModel
from dataclasses import dataclass

from typing import Optional, List, Literal, Type, Tuple, TypeVar

from loguru import logger

from config import settings
from Helper import get_pretty_dict_json_no_sort

import mimetypes

mimetypes.init()

# try:
#     from openai.types.chat import ChatCompletionDeveloperMessageParam, ChatCompletionSystemMessageParam, \
#         ChatCompletionUserMessageParam, ChatCompletionAssistantMessageParam, ChatCompletionToolMessageParam
# except Exception as e:
#     logger.exception(e)
#     # logger.opt(exception=True).debug("Exception logged with debug level:")


T = TypeVar("T", bound=BaseModel)


class GoogleLLMModel(StrEnum):
    GEMINI_25_PRO = "gemini-2.5-pro"  # Eingabepreis	Kostenlos	1,25 $, Prompts mit ≤ 200.000 Tokens 2,50 $, Prompts mit > 200.000 Tokens Ausgabepreis (einschließlich Denk-Tokens)	Kostenlos	10,00 $, Prompts mit <= 200.000 Tokens 15,00 $, Prompts mit > 200.000 Tokens
    GEMINI_25_FLASH = "gemini-2.5-flash"  # Eingabepreis	Kostenlos	0,30 $ (Text / Bild / Video) 1,00 $ (Audio) Ausgabepreis (einschließlich Denk-Tokens)	Kostenlos	2,50 $
    GEMINI_30_PRO_PREVIEW = "gemini-3-pro-preview"  # Eingabepreis 2,00 $, Prompts mit ≤ 200.000 Tokens 4,00 $, Prompts mit > 200.000 Tokens    # Ausgabepreis (einschließlich Denk-Tokens)	12,00 $, Prompts <= 200.000 Tokens  18,00 $, Prompts > 200.000 Tokens    # Preis für Kontext-Caching	0,20 $, Prompts mit <= 200.000 Tokens  0,40 $, Prompts mit > 200.000 Tokens    4,50 $ / 1.000.000 Tokens pro Stunde (Speicherpreis)  # Fundierung mit der Google Suche	1.500 RPD (kostenlos), danach (demnächst verfügbar) 14 $ pro 1.000 Suchanfragen


class AnthropicLLMModel(StrEnum):
    # https://docs.claude.com/en/api/client-sdks
    CLAUDE_OPUS_41 = "claude-opus-4-1"  # sehr teuer Input $15 / MTok Output $75 / MTok
    CLAUDE_SONNET_45 = "claude-sonnet-4-5"  # Input $3 / MTok	Output	$15 / MTok
    CLAUDE_SONNET_40 = (
        "claude-sonnet-4-0"  # kostet soviel wie 4.5 ist aber schneller  Input $3 / MTok Output $15 / MTok
    )
    CLAUDE_SONNET_37 = "claude-3-7-sonnet-latest"  # Input $3 / MTok	Output	$15 / MTok
    CLAUDE_HAIKU_45 = "claude-haiku-4-5"  # HAS Thinking mode! Input $1 / MTok	Output $5 / MTok
    CLAUDE_HAIKU_35 = "claude-3-5-haiku-latest"  # NO Thinking mode Input $0.80 / MTok Output $4 / MTok


# class AIRequestResponse(DBObject):
#     __tablename__ = 'airequestresponse'
#
#     airequestresponseid: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"), default=uuid.uuid4)
#     requestdate: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
#     requestmsgs: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
#     response: Mapped[str] = mapped_column(sqlalchemy.UnicodeText(), nullable=False)
#     usedllm: Mapped[LLMModel] = mapped_column(Enum(
#             LLMModel,
#             create_constraint=True,
#             validate_strings=True,
#             name="llmmodel"
#         ),
#         server_default="gemini-2.5-pro"
#     )


@dataclass
class AIRequest[T]:
    prompt: str
    system_prompt: str | None = None
    target: Literal["google", "anthropic"] = "google"
    model: GoogleLLMModel | AnthropicLLMModel = GoogleLLMModel.GEMINI_25_FLASH
    response_schema: Optional[T | List[T]] = None
    enable_websearch: bool = False
    history: Optional[List[google.genai.types.Content]] = None
    image: Optional[Path] = None
    # use_pydantic_ai_with_anthropic_response_schema: bool = _USE_PYDANTIC_AI_FOR_ANTHROPIC_RESPONSE_SCHEMA

    # dbrecorder: Optional["AIRequestDBRecorder"] = None
    # aireqresp: Optional[AIRequestResponse] = None


# class AIRequestDBRecorder[T]:
#     @staticmethod
#     def record(session: Session,
#                airequest: AIRequest[T],
#                answer: None|str = None,
#                rawresponse: Optional[BetaMessage|GenerateContentResponse] = None) -> AIRequestResponse:
#
#         # in history sind bei google NICHT die system_prompt drin...
#         torecord_msgs: List[Any] = []
#         if airequest.history:
#             ...
#
#         aireqresp: AIRequestResponse = AIRequestResponse(
#             airequestresponseid=uuid.uuid4(),
#             requestdate=datetime.datetime.now(tz=TIMEZONE),
#             requestmsgs=[{"role": "system", "content": "you are blarg"}, {"role": "user", "content": "what am i?"}],
#             response="you might be blargh or not.",
#             usedllm=airequest.model  # type: ignore
#         )
#         session.add(aireqresp)
#         session.commit()
#
#         airequest.aireqresp = aireqresp
#
#         return aireqresp


def request_gemini[T](
    airequest: AIRequest[T], debug_with_replayid: str | None = None, retries: int | None = 10
) -> Tuple[str | T | List[T], Optional[str], google.genai.types.GenerateContentResponse]:

    debugconfig: DebugConfig | None = None
    if debug_with_replayid:
        debugconfig = google.genai.client.DebugConfig(
            client_mode="record",
            replay_id=f"MODULE/FUNCTION/{datetime.datetime.now():%Y%m%d-%H%M%S.%s}",
            replays_directory=str(Path(Path(Path.home(), "Desktop"), "GOOGLE_REPLAYDIR")),
        )

    # https://googleapis.github.io/python-genai/genai.html#genai.types.HttpRetryOptions
    # https://github.com/googleapis/python-genai/issues/336
    http_options: google.genai.types.HttpOptions | None = None
    if retries is not None and retries > 1:
        http_options = google.genai.types.HttpOptions(
            retry_options=google.genai.types.HttpRetryOptions(
                initial_delay=5, attempts=retries, exp_base=2.0, max_delay=120.0, http_status_codes=[429, 502, 503, 504]
            )
        )

    client = google.genai.Client(
        http_options=http_options, api_key=settings.google.gemini_api_key, debug_config=debugconfig
    )

    grounding_tool: google.genai.types.Tool = google.genai.types.Tool(google_search=google.genai.types.GoogleSearch())

    thinking_config: google.genai.types.ThinkingConfig = google.genai.types.ThinkingConfig(
        thinking_budget=-1, include_thoughts=True  # 8192,
    )  # turn-off thinking: budget=0, dynamic thinking: budget=-1

    # google.genai.errors.ClientError: 400 INVALID_ARGUMENT.
    # {'error': {'code': 400, 'message': 'You can only set only one of thinking budget and thinking level.', 'status': 'INVALID_ARGUMENT'}}

    if airequest.model == GoogleLLMModel.GEMINI_30_PRO_PREVIEW:
        thinking_config.thinking_level = google.genai.types.ThinkingLevel.HIGH  # LOW | THINKING_LEVEL_UNSPECIFIED
        thinking_config.thinking_budget = None

    genconf: google.genai.types.GenerateContentConfig = google.genai.types.GenerateContentConfig(
        # system_instruction="You are a cat. Your name is Neko.",
        tools=[grounding_tool] if airequest.enable_websearch else None,
        thinking_config=thinking_config,
        response_mime_type="application/json" if airequest.response_schema else None,
        system_instruction=airequest.system_prompt,
        response_schema=airequest.response_schema,  # list[Recipe]
    )

    contents: List[google.genai.types.Content] = []
    if airequest.history:
        contents.extend(airequest.history)

    parts: List[google.genai.types.Part] = [google.genai.types.Part(text=airequest.prompt)]

    if airequest.image is not None:
        mt: str|None = mimetypes.guess_file_type(airequest.image)[0]
        assert mt is not None
        parts.append(google.genai.types.Part.from_bytes(data=airequest.image.read_bytes(), mime_type=mt))

    contents.append(google.genai.types.UserContent(parts=parts))

    logger.debug(f"{airequest.model=}")

    response: GenerateContentResponse = client.models.generate_content(
        model=airequest.model,
        contents=contents,  # type: ignore # why type-fail-check ?! # prompt,
        config=genconf,
    )

    # logger.debug(response.text)
    # logger.debug(get_pretty_dict_json_no_sort(response.to_json_dict()))

    mystuff: Optional[T | Type[List[T]]] = None
    # Use instantiated objects.
    if airequest.response_schema:
        mystuff = response.parsed  # type: ignore  # TODO HT20251126 make it properly typed
        # logger.debug(f"{type(mystuff)=} {mystuff=}")
        #
        # if isinstance(mystuff, list):
        #     for stuff in mystuff:
        #         logger.debug(stuff.model_dump_json())
        # else:
        #     logger.debug(mystuff.model_dump_json())

    answer: Optional[str] = None
    thoughts: Optional[str] = None

    thought_writer: StringIO = StringIO()
    answer_writer: StringIO = StringIO()

    # THIS WHOLE BLOCK IS NEEDED FOR MYPY TO BE HAPPY ?!
    assert response is not None and response.candidates is not None
    assert len(response.candidates) > 0 and response.candidates[0].content is not None and response.candidates[0].content.parts is not None

    for part in response.candidates[0].content.parts:
        if not part.text:
            continue

        if part.thought:
            # logger.debug("Thought summary:")
            # logger.debug(part.text)
            thought_writer.write(part.text.strip())
            thought_writer.write("\n")
        else:
            # logger.debug("Answer:")
            # logger.debug(part.text)
            # answer = part.text
            answer_writer.write(part.text.strip())
            answer_writer.write("\n")

    if answer_writer.tell() > 0:
        answer = answer_writer.getvalue().strip()

    if thought_writer.tell() > 0:
        thoughts = thought_writer.getvalue().strip()

    if mystuff:
        return mystuff, thoughts, response  # type: ignore  # TODO HT20251126 make it properly typed

    return answer, thoughts, response  # type: ignore  # TODO HT20251126 make it properly typed


def request_ai[T](
    airequest: AIRequest[T],
) -> Tuple[Optional[str] | T | List[T], Optional[str], Optional[GenerateContentResponse]]:
    answer: Optional[str] | T | List[T] = None
    thoughts: Optional[str] = None
    # rawresponse: Optional[BetaMessage|GenerateContentResponse] = None
    rawresponse: Optional[GenerateContentResponse] = None

    if airequest.target == "google":
        if not isinstance(airequest.model, GoogleLLMModel):
            raise ValueError(
                f"When target is 'google', the model must be an instance of GoogleLLMModel, but is {airequest.model=}"
            )

        answer, thoughts, rawresponse = request_gemini(airequest=airequest)

    elif airequest.target == "anthropic":
        raise NotImplementedError("Anthropic adapter taken out... aka not implemented here...")

    # if airequest.dbrecorder:
    #     with Session(bind=DBConnectionEngine.get_instance().get_engine(autocommit=True)) as session:
    #         airequest.dbrecorder.record(
    #             session=session,
    #             airequest=airequest,
    #             answer=answer,
    #             rawresponse=rawresponse
    #         )

    return answer, thoughts, rawresponse


def do_test_reqest() -> None:
    class Recipe(BaseModel):
        recipe_name: str
        ingredients: list[str]
        contained_in_this_many_websites: int

    class RecipeList(BaseModel):
        recipes: List[Recipe]

    recipelist: RecipeList
    thoughts: str
    # rawresponse: BetaMessage
    rawresponse_google: GenerateContentResponse

    prompt1: str = (
        "List 3 popular cookie recipes, and include the amounts of ingredients. Make an estimation of on how many websites this recipe is shared."
    )
    prompt2: str = "That is quite ok, but now make those recipes halloween-style."

    system_prompt: str | None = (
        "You are a some kind of a naughty minded pirate and include some awfull jokes and some pirate-speak into your answers."
    )

    history: Optional[List[google.genai.types.Content]] = None

    # NOTE: GEMINI_30_PRO_PREVIEW can use response_schema ("tool") and websearch simultaneously -> previous models CANNOT!!!
    for prompt in [prompt1, prompt2]:
        logger.debug(f"Now asking this Prompt:\n\t{prompt}")
        airequest: AIRequest = AIRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            target="google",
            model=GoogleLLMModel.GEMINI_30_PRO_PREVIEW,  # GEMINI_25_FLASH
            response_schema=RecipeList,
            enable_websearch=True,
            # response_schema=None,
            # enable_websearch=True,
            history=history,
            # use_pydantic_ai_with_anthropic_response_schema=use_pydantic_ai_with_anthropic_response_schema,
            # dbrecorder=dbrecorder
        )

        recipelist, thoughts, rawresponse_google = request_ai(airequest=airequest)  # type: ignore  # TODO HT20251126 make it properly typed

        # recipelist, thoughts, rawresponse = request_ai(target="anthropic",
        #                                model=AnthropicLLMModel.CLAUDE_SONNET_40,
        #                                # prompt="Search the internet (use websearch for that) for the 3 most popular cookie recipes, and include the amounts of ingredients.",
        #                                prompt="List 3 popular cookie recipes, and include the amounts of ingredients. Make an estimation of on how many websites this recipe is shared.",
        #                                response_schema=RecipeList,
        #                                enable_websearch=False)

        logger.debug(f"{type(rawresponse_google)=}")
        logger.debug(get_pretty_dict_json_no_sort(rawresponse_google.model_dump()))

        logger.debug("Thoughts:")
        logger.debug(thoughts)

        if isinstance(recipelist, str):
            logger.debug(f"{type(recipelist)=}\n{recipelist}")
        else:
            for recipe in recipelist.recipes:
                logger.debug(f"{"*" * 10}")
                logger.debug(get_pretty_dict_json_no_sort(recipe.model_dump()))

        logger.debug(f"\n\t{"*" * 30}\n")

        if not history:
            history = []

        history.append(google.genai.types.UserContent(parts=[google.genai.types.Part(text=prompt)]))

        # THIS WHOLE BLOCK IS NEEDED FOR MYPY TO BE HAPPY ?!
        assert rawresponse_google is not None and rawresponse_google.candidates is not None
        assert len(rawresponse_google.candidates) > 0 and rawresponse_google.candidates[0].content is not None
        assert rawresponse_google.candidates[0].content.parts is not None

        history.append(rawresponse_google.candidates[0].content)


def do_test_image_request() -> None:
    class Object(BaseModel):
        name: str
        confidence: float
        attributes: str

    class ImageDescription(BaseModel):
        summary: str
        objects: List[Object]
        scene: str
        colors: List[str]
        time_of_day: Literal["Morning", "Afternoon", "Evening", "Night"]
        setting: Literal["Indoor", "Outdoor", "Unknown"]
        text_content: Optional[str] = None

    thoughts: str
    # rawresponse: BetaMessage
    rawresponse_google: GenerateContentResponse

    prompt: str = (
        "Describe the image in detail. Include information about the objects, scene, colors, time of day, setting, and any text content. If the image contains a receipt, list the items, amounts and prices on the receipt."
    )
    system_prompt: str | None = (
        "You are a some kind of a naughty minded pirate and include some awfull jokes and some pirate-speak into your answers."
    )

    history: Optional[List[google.genai.types.Content]] = None

    # NOTE: GEMINI_30_PRO_PREVIEW can use response_schema ("tool") and websearch simultaneously -> previous models CANNOT!!!

    logger.debug(f"Now asking this Prompt:\n\t{prompt}")
    airequest: AIRequest = AIRequest(
        prompt=prompt,
        system_prompt=system_prompt,
        target="google",
        model=GoogleLLMModel.GEMINI_30_PRO_PREVIEW,  # GEMINI_25_FLASH
        response_schema=ImageDescription,
        enable_websearch=False,
        history=history,
        image=Path(Path.home(), f"Desktop/traderjoes_h0auyjrjq1n1yshsez3z.jpg"),
    )

    imagedescription, thoughts, rawresponse_google = request_ai(airequest=airequest) # type: ignore  # TODO HT20251126 make it properly typed

    logger.debug(rawresponse_google)
    logger.debug(f"{"*"*40}")
    logger.debug(thoughts)
    logger.debug(f"{"*"*40}")
    logger.debug(get_pretty_dict_json_no_sort(imagedescription.model_dump()))  # type: ignore  # TODO HT20251126 make it properly typed 

    # {
    #     "summary": "Ahoy, ye scurvy dogs! Feast yer one good eye on this here parchment of plunder from the merchant known as Trader Joe! It be restin' upon a wooden plank, likely the table in a captain's quarters. This scallywag has traded their doubloons for a bounty of provisions. Look at that! Six bananas? Har har har! Someone's got a hunger for the long, yellow fruit, eh? Maybe they be lonely on the high seas! And sour cream corn? Sounds like something that'd make ye walk the plank in the privy! Why couldn't the pirate play cards? Because he was standing on the deck! Arrrgh!",
    #     "objects": [
    #         {
    #             "name": "Receipt",
    #             "confidence": 0.99,
    #             "attributes": "White paper, printed text, listing groceries, Trader Joe's branded"
    #         },
    #         {
    #             "name": "Table Surface",
    #             "confidence": 0.95,
    #             "attributes": "Wooden, dark reddish-brown, scratched, varnished"
    #         }
    #     ],
    #     "scene": "A top-down close-up view of a grocery receipt lying on a dark wooden table.",
    #     "colors": [
    #         "White",
    #         "Black",
    #         "Brown",
    #         "Red"
    #     ],
    #     "time_of_day": "Morning",
    #     "setting": "Indoor",
    #     "text_content": "TRADER JOE'S\n\n785 Oak Grove Road\nConcord, CA       94518\nStore #0083 - 925 521-1134\n\nSALE TRANSACTION\n\nSOUR CREAM & ONION CORN   $2.49\nSLICED WHOLE WHEAT BREAD  $2.49\nRICE CAKES KOREAN TTEOK   $3.99\nSQUASH ZUCCHINI 1.5 LB    $2.49\nGREENS KALE 10 OZ         $1.99\nSQUASH SPAGHETTI EACH     $2.49\n50% LESS SALT ROASTED SA  $2.99\nBANANA EACH               $1.14\n6 @ $0.19\nPASTA GNOCCHI PRANZO      $1.99\nORG COCONUT MILK          $1.69\nORG YELLOW MUSTARD        $1.79\nHOL TRADITIONAL ACTIVE D  $1.29\n\nItems in Transaction:17\nBalance to pay            $26.83\nGift Card Tendered        $25.00\nVisa Debit                $1.83\n\nPAYMENT CARD PURCHASE TRANSACTION\nCUSTOMER COPY"
    # }


if __name__ == "__main__":
    # inputfile: Path = Path(Path.home(), f"Desktop/traderjoes_h0auyjrjq1n1yshsez3z.jpg")
    # mt: str = mimetypes.guess_file_type(inputfile)[0]
    #
    # print(f"{mt=} {inputfile.name[:-4]=}")

    # do_test_reqest()
    do_test_image_request()


# from google import genai
# from google.genai import types
# import base64
#
# # The media_resolution parameter is currently only available in the v1alpha API version.
# client = genai.Client(http_options={'api_version': 'v1alpha'})
#
# response = client.models.generate_content(
#     model="gemini-3-pro-preview",
#     contents=[
#         types.Content(
#             parts=[
#                 types.Part(text="What is in this image?"),
#                 types.Part(
#                     inline_data=types.Blob(
#                         mime_type="image/jpeg",
#                         data=base64.b64decode("..."),
#                     ),
#                     media_resolution={"level": "media_resolution_high"}
#                 )
#             ]
#         )
#     ]
# )
#
# print(response.text)
