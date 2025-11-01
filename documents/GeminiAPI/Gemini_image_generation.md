# Image generation

You can generate images using the Gemini API with either Gemini's built-in multimodal capabilities or Imagen, Google's specialized image generation model. For most use cases, start with [Gemini](https://ai.google.dev/gemini-api/docs/image-generation#gemini). Choose [Imagen](https://ai.google.dev/gemini-api/docs/image-generation#imagen) for specialized tasks where image quality is critical. See [Choosing the right model](https://ai.google.dev/gemini-api/docs/image-generation#choose-a-model) section for more guidance.

All generated images include a [SynthID watermark](https://ai.google.dev/responsible/docs/safeguards/synthid).

## Before you begin

Ensure you use a supported model and version for image generation:

- For **Gemini**, use Gemini 2.0 Flash Preview Image Generation.
    
- For **Imagen**, use Imagen 3. Note that this model is only available on the [Paid tier](https://ai.google.dev/gemini-api/docs/pricing).
    

You can access both Gemini and Imagen 3 using the same libraries.

**Note:** Image generation may not be available in all regions and countries, review our [Models](https://ai.google.dev/gemini-api/docs/models#gemini-2-5) page for more information.

## Generate images using Gemini

Gemini can generate and process images conversationally. You can prompt Gemini with text, images, or a combination of both to achieve various image-related tasks, such as image generation and editing.

You must include `responseModalities`: `["TEXT", "IMAGE"]` in your configuration. Image-only output is not supported with these models.

### Image generation (text-to-image)

The following code demonstrates how to generate an image based on a descriptive prompt:

```
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO
import base64

client = genai.Client()

contents = ('Hi, can you create a 3d rendered image of a pig '
            'with wings and a top hat flying over a happy '
            'futuristic scifi city with lots of greenery?')

response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=contents,
    config=types.GenerateContentConfig(
      response_modalities=['TEXT', 'IMAGE']
    )
)

for part in response.candidates[0].content.parts:
  if part.text is not None:
    print(part.text)
  elif part.inline_data is not None:
    image = Image.open(BytesIO((part.inline_data.data)))
    image.save('gemini-native-image.png')
    image.show()
```

**Note:** We've released the [Google SDK for TypeScript and JavaScript](https://www.npmjs.com/package/@google/genai) in [preview launch stage](https://github.com/googleapis/js-genai?tab=readme-ov-file#preview-launch). Use this SDK for image generation features.

### Image editing (text-and-image-to-image)

To perform image editing, add an image as input. The following example demonstrates uploading base64 encoded images. For multiple images and larger payloads, check the [image input](https://ai.google.dev/gemini-api/docs/image-understanding#image-input) section.

```
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO

import PIL.Image

image = PIL.Image.open('/path/to/image.png')

client = genai.Client()

text_input = ('Hi, This is a picture of me.'
            'Can you add a llama next to me?',)

response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=[text_input, image],
    config=types.GenerateContentConfig(
      response_modalities=['TEXT', 'IMAGE']
    )
)

for part in response.candidates[0].content.parts:
  if part.text is not None:
    print(part.text)
  elif part.inline_data is not None:
    image = Image.open(BytesIO(part.inline_data.data))
    image.show()
```

**Note:** We've released the [Google SDK for TypeScript and JavaScript](https://www.npmjs.com/package/@google/genai) in [preview launch stage](https://github.com/googleapis/js-genai?tab=readme-ov-file#preview-launch). Use this SDK for image generation features.

### Other image generation modes

Gemini supports other image interaction modes based on prompt structure and context, including:

- **Text to image(s) and text (interleaved):** Outputs images with related text.
    - Example prompt: "Generate an illustrated recipe for a paella."
- **Image(s) and text to image(s) and text (interleaved)**: Uses input images and text to create new related images and text.
    - Example prompt: (With an image of a furnished room) "What other color sofas would work in my space? can you update the image?"
- **Multi-turn image editing (chat):** Keep generating / editing images conversationally.
    - Example prompts: \[upload an image of a blue car.\] , "Turn this car into a convertible.", "Now change the color to yellow."

### Limitations

- For best performance, use the following languages: EN, es-MX, ja-JP, zh-CN, hi-IN.
- Image generation does not support audio or video inputs.
- Image generation may not always trigger:
    - The model may output text only. Try asking for image outputs explicitly (e.g. "generate an image", "provide images as you go along", "update the image").
    - The model may stop generating partway through. Try again or try a different prompt.
- When generating text for an image, Gemini works best if you first generate the text and then ask for an image with the text.
- There are some regions/countries where Image generation is not available. See [Models](https://ai.google.dev/gemini-api/docs/models) for more information.

## Generate images using Imagen 3

This example demonstrates generating images with [Imagen 3](https://deepmind.google/technologies/imagen-3/):

```
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO

client = genai.Client(api_key='GEMINI_API_KEY')

response = client.models.generate_images(
    model='imagen-3.0-generate-002',
    prompt='Robot holding a red skateboard',
    config=types.GenerateImagesConfig(
        number_of_images= 4,
    )
)
for generated_image in response.generated_images:
  image = Image.open(BytesIO(generated_image.image.image_bytes))
  image.show()
```

### Imagen model parameters

Imagen supports English only prompts at this time and the following parameters:

**Note:** Naming conventions of parameters vary by programming language.

- `numberOfImages`: The number of images to generate, from 1 to 4 (inclusive). The default is 4.
- `aspectRatio`: Changes the aspect ratio of the generated image. Supported values are `"1:1"`, `"3:4"`, `"4:3"`, `"9:16"`, and `"16:9"`. The default is `"1:1"`.
- `personGeneration`: Allow the model to generate images of people. The following values are supported:
    
    - `"dont_allow"`: Block generation of images of people.
    - `"allow_adult"`: Generate images of adults, but not children. This is the default.
    - `"allow_all"`: Generate images that include adults and children.
    
    **Note:** The "allow\_all" parameter value is not allowed in EU, UK, CH, MENA locations.
    

## Choosing the right model

Choose **Gemini** when:

- You need contextually relevant images that leverage world knowledge and reasoning.
- Seamlessly blending text and images is important.
- You want accurate visuals embedded within long text sequences.
- You want to edit images conversationally while maintaining context.

Choose **Imagen 3** when:

- Image quality, photorealism, artistic detail, or specific styles (e.g., impressionism, anime) are top priorities.
- Performing specialized editing tasks like product background updates or image upscaling.
- Infusing branding, style, or generating logos and product designs.

## Imagen prompt guide

This section of the Imagen guide shows you how modifying a text-to-image prompt can produce different results, along with examples of images you can create.
