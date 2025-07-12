# Adding server custom emoji support to the Discord bot.
import sys

from groq import AsyncGroq
from openai import AsyncOpenAI as OpenAI
from os import getenv
from dotenv import load_dotenv
from sys import exit
from utils.helpers import get_env_path, load_config, load_instructions
from utils.error_notifications import webhook_log, print_error
import random
import re

client = None
model = None
available_emojis = []


def init_ai():
    global client, model
    env_path = get_env_path()
    config = load_config()

    load_dotenv(dotenv_path=env_path)

    # Prioritize Groq over OpenAI if both are available
    if getenv("GROQ_API_KEY"):
        client = AsyncGroq(api_key=getenv("GROQ_API_KEY"))
        model = config["bot"]["groq_model"]
        print(f"Using Groq API with model: {model}")
    elif getenv("OPENAI_API_KEY"):
        client = OpenAI(api_key=getenv("OPENAI_API_KEY"))
        model = config["bot"]["openai_model"]
        print(f"Using OpenAI API with model: {model}")
    else:
        print("No API keys found, exiting.")
        sys.exit(1)


async def generate_response(prompt, instructions, history=None, guild_emojis=None):
    if not client:
        init_ai()

    try:
        # Load instructions from file instead of using passed parameter
        file_instructions = load_instructions()
        system_content = file_instructions if file_instructions else instructions

        if history:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt},
                    *history,
                ],
            )
        else:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt},
                ],
            )
        base_response = response.choices[0].message.content
        # Add random server emojis to make it more engaging
        if guild_emojis:
            enhanced_response = add_random_emojis(base_response, guild_emojis)
            return enhanced_response
        else:
            return base_response
    except Exception as e:
        print_error("AI Error", e)
        await webhook_log(None, e)
        return None


async def generate_response_image(prompt, instructions, image_url, history=None, guild_emojis=None):
    if not client:
        init_ai()
    try:
        image_response = await client.chat.completions.create(
            model="meta-llama/llama-4-maverick-17b-128e-instruct",  # [ ] make sure this works when user is using openai
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Describe / Explain in detail this image sent by a Discord user to an AI who will be responding to the message '{prompt}' based on your output as the AI cannot see the image. So make sure to tell the AI any key details about the image that you think are important to include in the response, especially any text on screen that the AI should be aware of.",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        )

        prompt_with_image = (
            f"{prompt} [Image of {image_response.choices[0].message.content}]"
        )

        if history:
            history.append({"role": "user", "content": prompt_with_image})

            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (load_instructions() if load_instructions() else instructions)
                        + " Images will be described to you, with the description wrapped in [|description|], so understand that you are to respond to the description as if it were an image you can see.",
                    },
                    {"role": "user", "content": prompt_with_image},
                    *history,
                ],
            )
        else:
            history = [{"role": "user", "content": prompt_with_image}]
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": load_instructions() if load_instructions() else instructions},
                    {"role": "user", "content": prompt_with_image},
                ],
            )
        history.append(
            {"role": "assistant", "content": response.choices[0].message.content}
        )
        base_response = response.choices[0].message.content
        # Add random server emojis to make it more engaging
        if guild_emojis:
            enhanced_response = add_random_emojis(base_response, guild_emojis)
            return enhanced_response
        else:
            return base_response
    except Exception as e:
        print_error("AI Error", e)
        await webhook_log(None, e)
        return None

def add_random_emojis(text, emojis):
    """
    Adds random emojis from the server to the given text.
    """
    num_emojis = random.randint(1, 3)  # Add 1 to 3 emojis
    emojis_to_add = random.sample(emojis, min(num_emojis, len(emojis)))

    words = text.split()
    num_words = len(words)

    for emoji in emojis_to_add:
        # Insert the emoji at a random position in the text
        insert_position = random.randint(0, num_words)
        words.insert(insert_position, str(emoji))  # Ensure emoji is a string

        num_words += 1  # Increment the number of words

    return " ".join(words)