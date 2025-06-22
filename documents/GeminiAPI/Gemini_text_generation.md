# Text generation

The Gemini API can generate text output from various inputs, including text, images, video, and audio, leveraging Gemini models.

Here's a basic example that takes a single text input:

```
from google import genai

client = genai.Client(api_key="GEMINI_API_KEY")

response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=["How does AI work?"]
)
print(response.text)
```

```
import { GoogleGenAI } from "@google/genai";

const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });

async function main() {
  const response = await ai.models.generateContent({
    model: "gemini-2.0-flash",
    contents: "How does AI work?",
  });
  console.log(response.text);
}

await main();
```

## System instructions and configurations

You can guide the behavior of Gemini models with system instructions. To do so, pass a [`GenerateContentConfig`](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig) object.

```
from google import genai
from google.genai import types

client = genai.Client(api_key="GEMINI_API_KEY")

response = client.models.generate_content(
    model="gemini-2.0-flash",
    config=types.GenerateContentConfig(
        system_instruction="You are a cat. Your name is Neko."),
    contents="Hello there"
)

print(response.text)
```

The [`GenerateContentConfig`](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig) object also lets you override default generation parameters, such as [temperature](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig).

```
from google import genai
from google.genai import types

client = genai.Client(api_key="GEMINI_API_KEY")

response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=["Explain how AI works"],
    config=types.GenerateContentConfig(
        max_output_tokens=500,
        temperature=0.1
    )
)
print(response.text)
```

Refer to the [`GenerateContentConfig`](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig) in our API reference for a complete list of configurable parameters and their descriptions.

## Multimodal inputs

The Gemini API supports multimodal inputs, allowing you to combine text with media files. The following example demonstrates providing an image:

```
from PIL import Image
from google import genai

client = genai.Client(api_key="GEMINI_API_KEY")

image = Image.open("/path/to/organ.png")
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=[image, "Tell me about this instrument"]
)
print(response.text)
```

For alternative methods of providing images and more advanced image processing, see our [image understanding guide](https://ai.google.dev/gemini-api/docs/image-understanding). The API also supports [document](https://ai.google.dev/gemini-api/docs/document-processing), [video](https://ai.google.dev/gemini-api/docs/video-understanding), and [audio](https://ai.google.dev/gemini-api/docs/audio) inputs and understanding.

## Streaming responses

By default, the model returns a response only after the entire generation process is complete.

For more fluid interactions, use streaming to receive [`GenerateContentResponse`](https://ai.google.dev/api/generate-content#v1beta.GenerateContentResponse) instances incrementally as they're generated.

```
from google import genai

client = genai.Client(api_key="GEMINI_API_KEY")

response = client.models.generate_content_stream(
    model="gemini-2.0-flash",
    contents=["Explain how AI works"]
)
for chunk in response:
    print(chunk.text, end="")
```

## Multi-turn conversations (Chat)

Our SDKs provide functionality to collect multiple rounds of prompts and responses into a chat, giving you an easy way to keep track of the conversation history.

**Note:** Chat functionality is only implemented as part of the SDKs. Behind the scenes, it still uses the [`generateContent`](https://ai.google.dev/api/generate-content#method:-models.generatecontent) API. For multi-turn conversations, the full conversation history is sent to the model with each follow-up turn.

```
from google import genai

client = genai.Client(api_key="GEMINI_API_KEY")
chat = client.chats.create(model="gemini-2.0-flash")

response = chat.send_message("I have 2 dogs in my house.")
print(response.text)

response = chat.send_message("How many paws are in my house?")
print(response.text)

for message in chat.get_history():
    print(f'role - {message.role}',end=": ")
    print(message.parts[0].text)
```

Streaming can also be used for multi-turn conversations.

```
from google import genai

client = genai.Client(api_key="GEMINI_API_KEY")
chat = client.chats.create(model="gemini-2.0-flash")

response = chat.send_message_stream("I have 2 dogs in my house.")
for chunk in response:
    print(chunk.text, end="")

response = chat.send_message_stream("How many paws are in my house?")
for chunk in response:
    print(chunk.text, end="")

for message in chat.get_history():
    print(f'role - {message.role}', end=": ")
    print(message.parts[0].text)
```

## Supported models

All models in the Gemini family support text generation. To learn more about the models and their capabilities, visit the [Models](https://ai.google.dev/gemini-api/docs/models) page.

## Best practices

### Prompting tips

For basic text generation, a [zero-shot](https://ai.google.dev/gemini-api/docs/prompting-strategies#few-shot) prompt often suffices without needing examples, system instructions or specific formatting.

For more tailored outputs:

- Use [System instructions](https://ai.google.dev/gemini-api/docs/text-generation?_gl=1*1v7cjgf*_up*MQ..*_ga*MjA2NDkxNDgxNS4xNzQ5MjcyNjU1*_ga_P1DBVKWT6V*czE3NDkyNzI2NTUkbzEkZzAkdDE3NDkyNzI2NTUkajYwJGwwJGgxODA0ODcxNzEy#system-instructions) to guide the model.
- Provide few example inputs and outputs to guide the model. This is often referred to as [few-shot](https://ai.google.dev/gemini-api/docs/prompting-strategies#few-shot) prompting.
- Consider [fine-tuning](https://ai.google.dev/gemini-api/docs/model-tuning) for advanced use cases.

Consult our [prompt engineering guide](https://ai.google.dev/gemini/docs/prompting-strategies) for more tips.

### Structured output

In some cases, you may need structured output, such as JSON. Refer to our [structured output](https://ai.google.dev/gemini-api/docs/structured-output) guide to learn how.

## What's next

- Try the [Gemini API getting started Colab](https://colab.research.google.com/github/google-gemini/cookbook/blob/main/quickstarts/Get_started.ipynb).
- Explore Gemini's [image](https://ai.google.dev/gemini-api/docs/image-understanding), [video](https://ai.google.dev/gemini-api/docs/video-understanding), [audio](https://ai.google.dev/gemini-api/docs/audio) and [document](https://ai.google.dev/gemini-api/docs/document-processing) understanding capabilities.
- Learn about multimodal [file prompting strategies](https://ai.google.dev/gemini-api/docs/files#prompt-guide).