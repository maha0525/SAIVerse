# Gemini thinking

The [Gemini 2.5 series models](https://ai.google.dev/gemini-api/docs/models) use an internal "thinking process" that significantly improves their reasoning and multi-step planning abilities, making them highly effective for complex tasks such as coding, advanced mathematics, and data analysis.

This guide shows you how to work with Gemini's thinking capabilities using the Gemini API.

## Before you begin

Ensure you use a supported 2.5 series model for thinking. You might find it beneficial to explore these models in AI Studio before diving into the API:

- [Try Gemini 2.5 Flash Preview in AI Studio](https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-preview-05-20)
- [Try Gemini 2.5 Pro Preview in AI Studio](https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-pro-preview-06-05)

**Note:** Thinking is **_enabled by default_** for the 2.5 series models. Read the section on [setting a thinking budget](https://ai.google.dev/gemini-api/docs/thinking#set-budget) for details and configuration.

## Generating content with thinking

Initiating a request with a thinking model is similar to any other content generation request. The key difference lies in specifying one of the [models with thinking support](https://ai.google.dev/gemini-api/docs/thinking?_gl=1*1n3urn*_up*MQ..*_ga*ODYxNTI1OS4xNzQ5OTkxNDc4*_ga_P1DBVKWT6V*czE3NDk5OTE0NzgkbzEkZzAkdDE3NDk5OTE0NzgkajYwJGwwJGgxMDIwNzI3Nzk4#supported-models) in the `model` field, as demonstrated in the following [text generation](https://ai.google.dev/gemini-api/docs/text-generation#text-input) example:

```
from google import genai

client = genai.Client(api_key="GOOGLE_API_KEY")
prompt = "Explain the concept of Occam's Razor and provide a simple, everyday example."
response = client.models.generate_content(
    model="gemini-2.5-pro-preview-06-05",
    contents=prompt
)

print(response.text)
```

## Thought summaries (Experimental)

Thought summaries offer insights into the model's internal reasoning process. This feature can be valuable for verifying the model's approach and keeping users informed during longer tasks, especially when combined with [streaming](https://ai.google.dev/gemini-api/docs/thinking?_gl=1*1n3urn*_up*MQ..*_ga*ODYxNTI1OS4xNzQ5OTkxNDc4*_ga_P1DBVKWT6V*czE3NDk5OTE0NzgkbzEkZzAkdDE3NDk5OTE0NzgkajYwJGwwJGgxMDIwNzI3Nzk4#streaming).

You can enable thought summaries by setting `includeThoughts` to `true` in your request configuration. You can then access the summary by iterating through the `response` parameter's `parts`, and checking the `thought` boolean.

Here's an example demonstrating how to enable and retrieve thought summaries without streaming, which returns a single, final thought summary with the response:

```
from google import genai
from google.genai import types

client = genai.Client(api_key="GOOGLE_API_KEY")
prompt = "What is the sum of the first 50 prime numbers?"
response = client.models.generate_content(
  model="gemini-2.5-pro-preview-06-05",
  contents=prompt,
  config=types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(
      include_thoughts=True
    )
  )
)

for part in response.candidates[0].content.parts:
  if not part.text:
    continue
  if part.thought:
    print("Thought summary:")
    print(part.text)
    print()
  else:
    print("Answer:")
    print(part.text)
    print()
```

And here is an example using thinking with streaming, which returns rolling, incremental summaries during generation:

```
from google import genai
from google.genai import types

client = genai.Client(api_key="GOOGLE_API_KEY")

prompt = """
Alice, Bob, and Carol each live in a different house on the same street: red, green, and blue.
The person who lives in the red house owns a cat.
Bob does not live in the green house.
Carol owns a dog.
The green house is to the left of the red house.
Alice does not own a cat.
Who lives in each house, and what pet do they own?
"""

thoughts = ""
answer = ""

for chunk in client.models.generate_content_stream(
    model="gemini-2.5-pro-preview-06-05",
    contents=prompt,
    config=types.GenerateContentConfig(
      thinking_config=types.ThinkingConfig(
        include_thoughts=True
      )
    )
):
  for part in chunk.candidates[0].content.parts:
    if not part.text:
      continue
    elif part.thought:
      if not thoughts:
        print("Thoughts summary:")
      print(part.text)
      thoughts += part.text
    else:
      if not answer:
        print("Thoughts summary:")
      print(part.text)
      answer += part.text
```

## Thinking budgets

The `thinkingBudget` parameter lets you guide the model on the number of thinking tokens it can use when generating a response. A higher token count generally allows for more detailed reasoning, which can be beneficial for tackling more [complex tasks](https://ai.google.dev/gemini-api/docs/thinking?_gl=1*1n3urn*_up*MQ..*_ga*ODYxNTI1OS4xNzQ5OTkxNDc4*_ga_P1DBVKWT6V*czE3NDk5OTE0NzgkbzEkZzAkdDE3NDk5OTE0NzgkajYwJGwwJGgxMDIwNzI3Nzk4#tasks). If you don't set the `thinkingBudget`, the model will dynamically adjust the budget based on the complexity of the request.

The `thinkingBudget` is only [supported](https://ai.google.dev/gemini-api/docs/thinking?_gl=1*1n3urn*_up*MQ..*_ga*ODYxNTI1OS4xNzQ5OTkxNDc4*_ga_P1DBVKWT6V*czE3NDk5OTE0NzgkbzEkZzAkdDE3NDk5OTE0NzgkajYwJGwwJGgxMDIwNzI3Nzk4#supported-models) in Gemini 2.5 Flash and 2.5 Pro. Depending on the prompt, the model might overflow or underflow the token budget.

The following are configuration requirements for each model type.

**Gemini 2.5 Pro**

- The `thinkingBudget` must be an integer in the range `128` to `32768`.
- You cannot turn thinking off when using Gemini 2.5 Pro, the lowest budget is `128`.
- If the `thinkingBudget` is not set, the model will automatically decide how much thinking budget to use.

**Gemini 2.5 Flash**

- The `thinkingBudget` must be an integer in the range `0` to `24576`.
- Setting the thinking budget to `0` disables thinking.

```
from google import genai
from google.genai import types

client = genai.Client()

response = client.models.generate_content(
    model="gemini-2.5-pro-preview-06-05",
    contents="Provide a list of 3 famous physicists and their key contributions",
    config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=1024)
    ),
)

print(response.text)
```

## Pricing

**Note:** Summaries are available in the [free and paid tiers](https://ai.google.dev/gemini-api/docs/pricing) of the API.

When thinking is turned on, response pricing is the sum of output tokens and thinking tokens. You can get the total number of generated thinking tokens from the `thoughtsTokenCount` field.

```
# ...
print("Thoughts tokens:",response.usage_metadata.thoughts_token_count)
print("Output tokens:",response.usage_metadata.candidates_token_count)
```

Thinking models generate full thoughts to improve the quality of the final response, and then output [summaries](https://ai.google.dev/gemini-api/docs/thinking?_gl=1*1n3urn*_up*MQ..*_ga*ODYxNTI1OS4xNzQ5OTkxNDc4*_ga_P1DBVKWT6V*czE3NDk5OTE0NzgkbzEkZzAkdDE3NDk5OTE0NzgkajYwJGwwJGgxMDIwNzI3Nzk4#summaries) to provide insight into the thought process. So, pricing is based on the full thought tokens the model needs to generate to create a summary, despite only the summary being output from the API.

You can learn more about tokens in the [Token counting](https://ai.google.dev/gemini-api/docs/tokens) guide.

## Supported Models

You can find all model capabilities on the [model overview](https://ai.google.dev/gemini-api/docs/models) page.

| Model | Thinking summaries | Thinking budget |
| --- | --- | --- |
| [Gemini 2.5 Flash](https://ai.google.dev/gemini-api/docs/models#gemini-2.5-flash-preview-05-20) | ✔️ | ✔️ |
| [Gemini 2.5 Pro](https://ai.google.dev/gemini-api/docs/models#gemini-2.5-pro-preview-06-05) | ✔️ | ✔️ |

## Best practices

This section includes some guidance for using thinking models efficiently. As always, following our [prompting guidance and best practices](https://ai.google.dev/gemini-api/docs/prompting-strategies) will get you the best results.

### Debugging and steering

- **Review reasoning**: When you're not getting your expected response from the thinking models, it can help to carefully analyze Gemini's reasoning process. You can see how it broke down the task and arrived at its conclusion, and use that information to correct towards the right results.
    
- **Provide Guidance in Reasoning**: If you're hoping for a particularly lengthy output, you may want to provide guidance in your prompt to constrain the [amount of thinking](https://ai.google.dev/gemini-api/docs/thinking?_gl=1*1n3urn*_up*MQ..*_ga*ODYxNTI1OS4xNzQ5OTkxNDc4*_ga_P1DBVKWT6V*czE3NDk5OTE0NzgkbzEkZzAkdDE3NDk5OTE0NzgkajYwJGwwJGgxMDIwNzI3Nzk4#set-budget) the model uses. This lets you reserve more of the token output for your response.
    

### Task complexity

- **Easy Tasks (Thinking could be OFF):** For straightforward requests where complex reasoning isn't required, such as fact retrieval or classification, thinking is not required. Examples include:
    - "Where was DeepMind founded?"
    - "Is this email asking for a meeting or just providing information?"
- **Medium Tasks (Default/Some Thinking):** Many common requests benefit from a degree of step-by-step processing or deeper understanding. Gemini can flexibly use thinking capability for tasks like:
    - Analogize photosynthesis and growing up.
    - Compare and contrast electric cars and hybrid cars.
- **Hard Tasks (Maximum Thinking Capability):** For truly complex challenges, such as solving complex math problems or coding tasks, we recommend setting a high thinking budget. These types of tasks require the model needs to engage its full reasoning and planning capabilities, often involving many internal steps before providing an answer. Examples include:
    - Solve problem 1 in AIME 2025: Find the sum of all integer bases b > 9 for which 17<sub>b</sub> is a divisor of 97<sub>b</sub>.
    - Write Python code for a web application that visualizes real-time stock market data, including user authentication. Make it as efficient as possible.

## Thinking with tools and capabilities

Thinking models work with all of Gemini's tools and capabilities. This allows the models to interact with external systems, execute code, or access real-time information, incorporating the results into their reasoning and final response.

- The [search tool](https://ai.google.dev/gemini-api/docs/grounding) allows the model to query Google Search to find up-to-date information or information beyond its training data. This is useful for questions about recent events or highly specific topics.
    
- The [code execution tool](https://ai.google.dev/gemini-api/docs/code-execution) enables the model to generate and run Python code to perform calculations, manipulate data, or solve problems that are best handled algorithmically. The model receives the code's output and can use it in its response.
    
- With [structured output](https://ai.google.dev/gemini-api/docs/structured-output), you can constrain Gemini to respond with JSON. This is particularly useful for integrating the model's output into applications.
    
- [Function calling](https://ai.google.dev/gemini-api/docs/function-calling) connects the thinking model to external tools and APIs, so it can reason about when to call the right function and what parameters to provide.
    

You can try examples of using tools with thinking models in the [Thinking cookbook](https://colab.sandbox.google.com/github/google-gemini/cookbook/blob/main/quickstarts/Get_started_thinking.ipynb).