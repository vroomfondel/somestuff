import time
from google import genai
from google.genai.types import CreateBatchJobConfig
from pydantic import BaseModel, TypeAdapter


class Recipe(BaseModel):
    recipe_name: str
    ingredients: list[str]


client = genai.Client()

jc: CreateBatchJobConfig


# A list of dictionaries, where each is a GenerateContentRequest
inline_requests = [
    {
        "contents": [
            {
                "parts": [{"text": "List a few popular cookie recipes, and include the amounts of ingredients."}],
                "role": "user",
            }
        ],
        "config": {"response_mime_type": "application/json", "response_schema": list[Recipe]},
    },
    {
        "contents": [
            {
                "parts": [
                    {"text": "List a few popular gluten free cookie recipes, and include the amounts of ingredients."}
                ],
                "role": "user",
            }
        ],
        "config": {"response_mime_type": "application/json", "response_schema": list[Recipe]},
    },
]

inline_batch_job = client.batches.create(
    model="models/gemini-2.5-flash",
    src=inline_requests,
    config={"display_name": "structured-output-job-1"},
)

# wait for the job to finish
job_name = inline_batch_job.name
print(f"Polling status for job: {job_name}")

while True:
    batch_job_inline = client.batches.get(name=job_name)
    if batch_job_inline.state.name in (
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
    ):
        break
    print(f"Job not finished. Current state: {batch_job_inline.state.name}. Waiting 30 seconds...")
    time.sleep(30)

print(f"Job finished with state: {batch_job_inline.state.name}")

# print the response
for i, inline_response in enumerate(batch_job_inline.dest.inlined_responses, start=1):
    print(f"\n--- Response {i} ---")

    # Check for a successful response
    if inline_response.response:
        # The .text property is a shortcut to the generated text.
        print(inline_response.response.text)


import time
from google import genai

client = genai.Client()

# Use the name of the job you want to check
# e.g., inline_batch_job.name from the previous step
job_name = "YOUR_BATCH_JOB_NAME"  # (e.g. 'batches/your-batch-id')
batch_job = client.batches.get(name=job_name)

completed_states = set(
    [
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
    ]
)

print(f"Polling status for job: {job_name}")
batch_job = client.batches.get(name=job_name)  # Initial get
while batch_job.state.name not in completed_states:
    print(f"Current state: {batch_job.state.name}")
    time.sleep(30)  # Wait for 30 seconds before polling again
    batch_job = client.batches.get(name=job_name)

print(f"Job finished with state: {batch_job.state.name}")
if batch_job.state.name == "JOB_STATE_FAILED":
    print(f"Error: {batch_job.error}")


import json
from google import genai

client = genai.Client()

# Use the name of the job you want to check
# e.g., inline_batch_job.name from the previous step
job_name = "YOUR_BATCH_JOB_NAME"
batch_job = client.batches.get(name=job_name)

if batch_job.state.name == "JOB_STATE_SUCCEEDED":

    # If batch job was created with a file
    if batch_job.dest and batch_job.dest.file_name:
        # Results are in a file
        result_file_name = batch_job.dest.file_name
        print(f"Results are in file: {result_file_name}")

        print("Downloading result file content...")
        file_content = client.files.download(file=result_file_name)
        # Process file_content (bytes) as needed
        print(file_content.decode("utf-8"))

    # If batch job was created with inline request
    # (for embeddings, use batch_job.dest.inlined_embed_content_responses)
    elif batch_job.dest and batch_job.dest.inlined_responses:
        # Results are inline
        print("Results are inline:")
        for i, inline_response in enumerate(batch_job.dest.inlined_responses):
            print(f"Response {i+1}:")
            if inline_response.response:
                # Accessing response, structure may vary.
                try:
                    print(inline_response.response.text)
                except AttributeError:
                    print(inline_response.response)  # Fallback
            elif inline_response.error:
                print(f"Error: {inline_response.error}")
    else:
        print("No results found (neither file nor inline).")
else:
    print(f"Job did not succeed. Final state: {batch_job.state.name}")
    if batch_job.error:
        print(f"Error: {batch_job.error}")
