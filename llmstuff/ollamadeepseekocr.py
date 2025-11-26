from pathlib import Path
from typing import Iterator, Dict, List
import json

import ollama
from loguru import logger

import base64

import difflib

from ollama import GenerateResponse

# if TYPE_CHECKING:
#     from _typeshed import SupportsWrite
# from io import StringIO
# from jinja2 import Environment, FileSystemLoader

_OLLAMA_HTTPX_CLIENT_TIMEOUT: float | None = None

OLLAMA_HOST = "http://127.0.0.1:11434"

logger.debug(f"OLLAMA_HOST: {OLLAMA_HOST}")

OLLAMA_CLIENT = ollama.Client(host=OLLAMA_HOST, timeout=_OLLAMA_HTTPX_CLIENT_TIMEOUT)

OCRTESTFILE_JPG: Path = Path(Path.home(), f"Desktop/traderjoes_h0auyjrjq1n1yshsez3z.jpg")

OCR_MODEL = "deepseek-ocr:latest"

DEFAULT_OCR_SYSTEM_PROMPT = """Act as an OCR assistant. Analyze the provided image and:
    1. Recognize all visible text in the image as accurately as possible.
    2. Maintain the original structure and formatting of the text.
    3. If any words or phrases are unclear, indicate this with [unclear] in your transcription.

    Provide only the transcription without any additional comments."""

DEFAULT_MARKDOWN_SYSTEM_PROMPT = """Convert the provided image into Markdown format. Ensure that all content from the page is included, such as headers, footers, subtexts, images (with alt text if possible), tables, and any other elements.

  Requirements:
  - Output Only Markdown: Return solely the Markdown content without any additional explanations or comments.
  - No Delimiters: Do not use code fences or delimiters like ```markdown.
  - Complete Content: Do not omit any part of the page, including headers, footers, and subtext."""


def runocr(imagefile: Path) -> str:
    image_bytes: bytes = imagefile.read_bytes()
    image_b64: str = base64.b64encode(image_bytes).decode("utf-8")

    response: GenerateResponse | Iterator[GenerateResponse] = OLLAMA_CLIENT.generate(
        # system=DEFAULT_OCR_SYSTEM_PROMPT,  # ?! really needed? deepseek-ocr seems to be a bit picky
        model=OCR_MODEL,
        # prompt="<image>\n<|grounding|>Convert the document to markdown.",  # https://ollama.com/library/deepseek-ocr
        prompt="<image>\n<|grounding|>Convert the document to markdown and count the bananas.",
        images=[image_b64],
        stream=False,
    )
    if isinstance(response, Iterator):
        raise Exception("Unexpectedly got an iterator")

    # logger.debug(response)
    return response.response


def comparedifferentfiletypes() -> List[tuple[str, str, float, int]]:
    # encoded = base64.b64encode(b'data to be encoded')

    # difflib.unified_diff(redo_resps[0], redo_resps[1],
    #                                   fromfile=f"NO_HISTORY", tofile=f"WITH_HISTORY"):

    result_lines: Dict[str, List[str] | None] = {"webp": None, "png": None, "jpg": None}
    result_texts: Dict[str, str | None] = {"webp": None, "png": None, "jpg": None}

    for suff in result_texts.keys():
        inputfile: Path = Path(OCRTESTFILE_JPG.parent, f"{OCRTESTFILE_JPG.name[:-4]}.{suff}")
        logger.debug(f"Input file: {inputfile}")

        resp: str = runocr(inputfile)

        logger.debug(f"Got OCR result for .{suff}:")
        logger.debug(resp)

        result_lines[suff] = resp.splitlines()
        result_texts[suff] = resp

    comparisons: List[tuple[str, str, float, int]] = []  # (from, to, ratio, changed_chars)

    for psuff in result_texts.keys():
        prevl: List[str] = result_lines[psuff]  # type: ignore
        prev: str = result_texts[psuff]  # type: ignore

        for suff in result_texts.keys():
            if psuff == suff:
                continue

            respl: List[str] = result_lines[suff]  # type: ignore
            resp = result_texts[suff]  # type: ignore

            matcher = difflib.SequenceMatcher(None, prev, resp)
            changed_chars: int = 0
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "replace":
                    changed_chars += max(i2 - i1, j2 - j1)
                elif tag == "delete":
                    changed_chars += i2 - i1
                elif tag == "insert":
                    changed_chars += j2 - j1

            # Berechne die Signifikanz der Änderungen
            max_total_chars: int = max(len(prev), len(resp))
            change_ratio: float = changed_chars / max_total_chars if max_total_chars > 0 else 0

            # Definiere einen Schwellenwert (z.B. 5% Änderungen)
            significance_threshold = 0.05
            is_significant = change_ratio > significance_threshold

            comparisons.append((psuff, suff, change_ratio, changed_chars))

            logger.debug(f"DIFF [{changed_chars=} {psuff} -> {suff}]:")
            logger.debug(f"Change ratio: {change_ratio:.2%} - {'SIGNIFICANT' if is_significant else 'MINOR'}")

            d = difflib.unified_diff(prevl, respl, fromfile=psuff, tofile=suff)

            unified_diff_str: str = "\n".join(d)

            logger.debug(unified_diff_str)

    comparisons.sort(key=lambda x: x[2])  # 2: ratio

    return comparisons


# TODO HT20251126 implement/check/validate this
def llm_semantic_comparison(text1: str, text2: str) -> Dict:
    """Lasse LLM die semantische Ähnlichkeit bewerten"""

    prompt = f"""Compare these two OCR results and provide a structured analysis:

TEXT 1:
{text1}

TEXT 2:
{text2}

Analyze:
1. Semantic similarity (0-100%): Do they convey the same meaning?
2. Critical differences: Are there factual errors (numbers, names, dates)?
3. Structural differences: Is formatting/layout preserved?
4. Overall quality score (0-100): Which text is more accurate?

Return ONLY valid JSON:
{{
  "semantic_similarity": <percentage>,
  "critical_differences": <count>,
  "structural_similarity": <percentage>,
  "better_result": "text1" or "text2",
  "confidence": <percentage>,
  "explanation": "<brief reason>"
}}"""

    response = OLLAMA_CLIENT.generate(
        model="deepseek-r1:latest", prompt=prompt, format="json", stream=False  # oder ein anderes reasoning model
    )

    return json.loads(response.response)


# TODO HT20251126 implement/check/validate this
def llm_categorize_differences(text1: str, text2: str, diff_str: str) -> Dict:
    """Lasse LLM die Unterschiede kategorisieren"""

    prompt = f"""You are analyzing differences between two OCR results of the same image.

DIFF:
{diff_str}

Categorize each difference as:
- CRITICAL: Numbers, amounts, names, dates changed
- MODERATE: Words with different meaning
- MINOR: Typos, punctuation, whitespace, capitalization
- NOISE: OCR artifacts, formatting only

Return JSON:
{{
  "critical_count": <number>,
  "moderate_count": <number>,
  "minor_count": <number>,
  "noise_count": <number>,
  "significance_score": <0-100>,
  "most_concerning": "<description of worst error if any>"
}}"""

    response = OLLAMA_CLIENT.generate(model="deepseek-r1:latest", prompt=prompt, format="json", stream=False)

    return json.loads(response.response)


# from pydantic import BaseModel
#
# class Pet(BaseModel):
#   name: str
#   animal: str
#   age: int
#   color: str | None
#   favorite_toy: str | None
#
# class PetList(BaseModel):
#   pets: list[Pet]

# pets = PetList.model_validate_json(response.message.content)


# TODO HT20251126 implement/check/validate this
def llm_ensemble_best_result(results: Dict[str, str], image_b64: str) -> str:
    """Lasse LLM aus mehreren OCR-Ergebnissen das beste auswählen"""

    results_text = "\n\n---\n\n".join([f"RESULT {fmt.upper()}:\n{text}" for fmt, text in results.items()])

    prompt = f"""You have {len(results)} OCR results of the same image. 
Analyze them and determine which is most accurate by:
1. Checking internal consistency
2. Identifying obvious OCR errors
3. Comparing completeness

RESULTS:
{results_text}

Return JSON with your analysis:
{{
  "best_format": "<format>",
  "confidence": <0-100>,
  "reasoning": "<why this is best>",
  "combined_text": "<your corrected/improved version>"
}}"""

    response = OLLAMA_CLIENT.generate(
        model=OCR_MODEL,
        prompt=f"<image>\n\n{prompt}",
        images=[image_b64],
        format="json",  # format=Country.model_json_schema(),
        stream=False,
    )

    return json.loads(response.response)


# def comprehensive_comparison(results: Dict[str, str], image_b64: str) -> Dict:
#     """Kombiniere mehrere Metriken für robuste Bewertung"""
#
#     comparisons = []
#
#     # Phase 1: Schnelle statistische Metriken
#     for fmt1, text1 in results.items():
#         for fmt2, text2 in results.items():
#             if fmt1 >= fmt2:
#                 continue
#
#             char_ratio = calculate_char_change_ratio(text1, text2)
#             weighted_ratio = calculate_weighted_changes(text1, text2)
#             struct_diff = structural_similarity(
#                 text1.splitlines(),
#                 text2.splitlines()
#             )
#
#             # Kombinierter Score
#             quick_score = (char_ratio * 0.4 +
#                            weighted_ratio * 0.4 +
#                            struct_diff * 0.2)
#
#             comparisons.append({
#                 'formats': (fmt1, fmt2),
#                 'quick_score': quick_score,
#                 'char_ratio': char_ratio,
#                 'weighted_ratio': weighted_ratio,
#                 'struct_diff': struct_diff
#             })
#
#     # Phase 2: LLM nur für kritische Fälle
#     # Wenn Unterschiede groß sind (>10%) oder unklar
#     critical_comparisons = [c for c in comparisons if c['quick_score'] > 0.1]
#
#     if critical_comparisons:
#         logger.info("Kritische Unterschiede gefunden - starte LLM-Analyse...")
#
#         for comp in critical_comparisons:
#             fmt1, fmt2 = comp['formats']
#             llm_analysis = llm_semantic_comparison(
#                 results[fmt1],
#                 results[fmt2]
#             )
#             comp['llm_analysis'] = llm_analysis
#
#     # Phase 3: Finale Empfehlung
#     best_format = llm_ensemble_best_result(results, image_b64)
#
#     return {
#         'comparisons': comparisons,
#         'recommendation': best_format
#     }


def main() -> None:
    comparisons: List[tuple[str, str, float, int]] = comparedifferentfiletypes()

    logger.info("\n=== TOPliste mit geringster Change Ratio ===")
    for i, (from_fmt, to_fmt, ratio, chars) in enumerate(comparisons, 1):
        logger.info(f"{i}. {from_fmt} -> {to_fmt}: {ratio:.2%} ({chars} Zeichen geändert)")


if __name__ == "__main__":
    main()

    # NOTE: this only accounts for sheer number of characters changed and not the significance of those changes...
    # TODO: HT20251126 implement some kind of measurement for the significance of the difference/changed characters
    # === TOPliste mit geringster Change Ratio ===
    # 2025-11-25 10:19:36.286 | INFO     | __main__:main:132 - 1. jpg -> webp: 6.46% (68 Zeichen geändert)
    # 2025-11-25 10:19:36.286 | INFO     | __main__:main:132 - 2. webp -> jpg: 6.93% (73 Zeichen geändert)
    # 2025-11-25 10:19:36.286 | INFO     | __main__:main:132 - 3. webp -> png: 22.76% (280 Zeichen geändert)
    # 2025-11-25 10:19:36.286 | INFO     | __main__:main:132 - 4. jpg -> png: 22.76% (280 Zeichen geändert)
    # 2025-11-25 10:19:36.286 | INFO     | __main__:main:132 - 5. png -> webp: 28.21% (347 Zeichen geändert)
    # 2025-11-25 10:19:36.286 | INFO     | __main__:main:132 - 6. png -> jpg: 38.21% (470 Zeichen geändert)
