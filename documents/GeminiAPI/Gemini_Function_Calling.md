# Function Calling with the Gemini API

Function calling lets you connect models to external tools and APIs. Instead of generating text responses, the model understands when to call specific functions and provides the necessary parameters to execute real-world actions. This allows the model to act as a bridge between natural language and real-world actions and data. Function calling has 3 primary use cases:

- **Augment Knowledge:** Access information from external sources like databases, APIs, and knowledge bases.
- **Extend Capabilities:** Use external tools to perform computations and extend the limitations of the model, such as using a calculator or creating charts.
- **Take Actions:** Interact with external systems using APIs, such as scheduling appointments, creating invoices, sending emails, or controlling smart home devices

Get Weather Schedule Meeting Create Chart

 ```
 from google import genai
 from google.genai import types

 # Define the function declaration for the model
 schedule_meeting_function = {
     "name": "schedule_meeting",
     "description": "Schedules a meeting with specified attendees at a given time and date.",
     "parameters": {
         "type": "object",
         "properties": {
             "attendees": {
                 "type": "array",
                 "items": {"type": "string"},
                 "description": "List of people attending the meeting.",
             },
             "date": {
                 "type": "string",
                 "description": "Date of the meeting (e.g., '2024-07-29')",
             },
             "time": {
                 "type": "string",
                 "description": "Time of the meeting (e.g., '15:00')",
             },
             "topic": {
                 "type": "string",
                 "description": "The subject or topic of the meeting.",
             },
         },
         "required": ["attendees", "date", "time", "topic"],
     },
 }

 # Configure the client and tools
 client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
 tools = types.Tool(function_declarations=[schedule_meeting_function])
 config = types.GenerateContentConfig(tools=[tools])

 # Send request with function declarations
 response = client.models.generate_content(
     model="gemini-2.0-flash",
     contents="Schedule a meeting with Bob and Alice for 03/14/2025 at 10:00 AM about the Q3 planning.",
     config=config,
 )

 # Check for a function call
 if response.candidates[0].content.parts[0].function_call:
     function_call = response.candidates[0].content.parts[0].function_call
     print(f"Function to call: {function_call.name}")
     print(f"Arguments: {function_call.args}")
     #  In a real app, you would call your function here:
     #  result = schedule_meeting(**function_call.args)
 else:
     print("No function call found in the response.")
     print(response.text)
```

## How Function Calling Works

![function calling
overview](/static/gemini-api/docs/images/function-calling-overview.png)

Function calling involves a structured interaction between your application, the model, and external functions. Here's a breakdown of the process:

1. **Define Function Declaration:** Define the function declaration in your application code. Function Declarations describe the function's name, parameters, and purpose to the model.
2. **Call LLM with function declarations:** Send user prompt along with the function declaration(s) to the model. It analyzes the request and determines if a function call would be helpful. If so, it responds with a structured JSON object.
3. **Execute Function Code (Your Responsibility):** The Model _does not_ execute the function itself. It's your application's responsibility to process the response and check for Function Call, if
    - **Yes**: Extract the name and args of the function and execute the corresponding function in your application.
    - **No:** The model has provided a direct text response to the prompt (this flow is less emphasized in the example but is a possible outcome).
4. **Create User friendly response:** If a function was executed, capture the result and send it back to the model in a subsequent turn of the conversation. It will use the result to generate a final, user-friendly response that incorporates the information from the function call.

This process can be repeated over multiple turns, allowing for complex interactions and workflows. The model also supports calling multiple functions in a single turn ([parallel function calling](https://ai.google.dev/gemini-api/docs/function-calling#parallel_function_calling)) and in sequence ([compositional function calling](https://ai.google.dev/gemini-api/docs/function-calling#compositional_function_calling)).

### Step 1: Define Function Declaration

Define a function and its declaration within your application code that allows users to set light values and make an API request. This function could call external services or APIs.

```
from google.genai import types

# Define a function that the model can call to control smart lights
set_light_values_declaration = {
    "name": "set_light_values",
    "description": "Sets the brightness and color temperature of a light.",
    "parameters": {
        "type": "object",
        "properties": {
            "brightness": {
                "type": "integer",
                "description": "Light level from 0 to 100. Zero is off and 100 is full brightness",
            },
            "color_temp": {
                "type": "string",
                "enum": ["daylight", "cool", "warm"],
                "description": "Color temperature of the light fixture, which can be `daylight`, `cool` or `warm`.",
            },
        },
        "required": ["brightness", "color_temp"],
    },
}

# This is the actual function that would be called based on the model's suggestion
def set_light_values(brightness: int, color_temp: str) -> dict[str, int | str]:
    """Set the brightness and color temperature of a room light. (mock API).

    Args:
        brightness: Light level from 0 to 100. Zero is off and 100 is full brightness
        color_temp: Color temperature of the light fixture, which can be `daylight`, `cool` or `warm`.

    Returns:
        A dictionary containing the set brightness and color temperature.
    """
    return {"brightness": brightness, "colorTemperature": color_temp}

```

### Step 2: Call the model with function declarations

Once you have defined your function declarations, you can prompt the model to use the function. It analyzes the prompt and function declarations and decides to respond directly or to call a function. If a function is called the response object will contain a function call suggestion.

```
from google import genai

# Generation Config with Function Declaration
tools = types.Tool(function_declarations=[set_light_values_declaration])
config = types.GenerateContentConfig(tools=[tools])

# Configure the client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Define user prompt
contents = [
    types.Content(
        role="user", parts=[types.Part(text="Turn the lights down to a romantic level")]
    )
]

# Send request with function declarations
response = client.models.generate_content(
    model="gemini-2.0-flash", config=config, contents=contents
)

print(response.candidates[0].content.parts[0].function_call)
```

The model then returns a `functionCall` object in an OpenAPI compatible schema specifying how to call one or more of the declared functions in order to respond to the user's question.


```
id=None args={'color_temp': 'warm', 'brightness': 25} name='set_light_values'
```

### Step 3: Execute set\_light\_values function code

Extract the function call details from the model's response, parse the arguments , and execute the `set_light_values` function in our code.


```
# Extract tool call details, it may not be in the first part.
tool_call = response.candidates[0].content.parts[0].function_call

if tool_call.name == "set_light_values":
    result = set_light_values(**tool_call.args)
    print(f"Function execution result: {result}")
```

### Step 4: Create User friendly response with function result and call the model again

Finally, send the result of the function execution back to the model so it can incorporate this information into its final response to the user.

```
# Create a function response part
function_response_part = types.Part.from_function_response(
    name=tool_call.name,
    response={"result": result},
)

# Append function call and result of the function execution to contents
contents.append(response.candidates[0].content) # Append the content from the model's response.
contents.append(types.Content(role="user", parts=[function_response_part])) # Append the function response

final_response = client.models.generate_content(
    model="gemini-2.0-flash",
    config=config,
    contents=contents,
)

print(final_response.text)
```

This completes the function calling flow. The Model successfully used the `set_light_values` function to perform the request action of the user.

## Function declarations

When you implement function calling in a prompt, you create a `tools` object, which contains one or more _`function declarations`_. You define functions using JSON, specifically with a [select subset](https://ai.google.dev/api/caching#Schema) of the [OpenAPI schema](https://spec.openapis.org/oas/v3.0.3#schemawr) format. A single function declaration can include the following parameters:

- `name` (string): A unique name for the function (`get_weather_forecast`, `send_email`). Use descriptive names without spaces or special characters (use underscores or camelCase).
- `description` (string): A clear and detailed explanation of the function's purpose and capabilities. This is crucial for the model to understand when to use the function. Be specific and provide examples if helpful ("Finds theaters based on location and optionally movie title which is currently playing in theaters.").
- `parameters` (object): Defines the input parameters the function expects.
    - `type` (string): Specifies the overall data type, such as `object`.
    - `properties` (object): Lists individual parameters, each with:
        - `type` (string): The data type of the parameter, such as `string`, `integer`, `boolean, array`.
        - `description` (string): A description of the parameter's purpose and format. Provide examples and constraints ("The city and state, e.g., 'San Francisco, CA' or a zip code e.g., '95616'.").
        - `enum` (array, optional): If the parameter values are from a fixed set, use "enum" to list the allowed values instead of just describing them in the description. This improves accuracy ("enum": \["daylight", "cool", "warm"\]).
    - `required` (array): An array of strings listing the parameter names that are mandatory for the function to operate.

## Parallel Function Calling

In addition to single turn function calling, you can also call multiple functions at once. Parallel function calling lets you execute multiple functions at once and is used when the functions are not dependent on each other. This is useful in scenarios like gathering data from multiple independent sources, such as retrieving customer details from different databases or checking inventory levels across various warehouses or performing multiple actions such as converting your apartment into a disco.


```
power_disco_ball = {
    "name": "power_disco_ball",
    "description": "Powers the spinning disco ball.",
    "parameters": {
        "type": "object",
        "properties": {
            "power": {
                "type": "boolean",
                "description": "Whether to turn the disco ball on or off.",
            }
        },
        "required": ["power"],
    },
}

start_music = {
    "name": "start_music",
    "description": "Play some music matching the specified parameters.",
    "parameters": {
        "type": "object",
        "properties": {
            "energetic": {
                "type": "boolean",
                "description": "Whether the music is energetic or not.",
            },
            "loud": {
                "type": "boolean",
                "description": "Whether the music is loud or not.",
            },
        },
        "required": ["energetic", "loud"],
    },
}

dim_lights = {
    "name": "dim_lights",
    "description": "Dim the lights.",
    "parameters": {
        "type": "object",
        "properties": {
            "brightness": {
                "type": "number",
                "description": "The brightness of the lights, 0.0 is off, 1.0 is full.",
            }
        },
        "required": ["brightness"],
    },
}
```

Call the model with an instruction that could use all of the specified tools. This example uses a `tool_config`. To learn more you can read about [configuring function calling](https://ai.google.dev/gemini-api/docs/function-calling#function_calling_modes).


```
from google import genai
from google.genai import types

# Set up function declarations
house_tools = [
    types.Tool(function_declarations=[power_disco_ball, start_music, dim_lights])
]

config = {
    "tools": house_tools,
    "automatic_function_calling": {"disable": True},
    # Force the model to call 'any' function, instead of chatting.
    "tool_config": {"function_calling_config": {"mode": "any"}},
}

# Configure the client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

chat = client.chats.create(model="gemini-2.0-flash", config=config)
response = chat.send_message("Turn this place into a party!")

# Print out each of the function calls requested from this single call
print("Example 1: Forced function calling")
for fn in response.function_calls:
    args = ", ".join(f"{key}={val}" for key, val in fn.args.items())
    print(f"{fn.name}({args})")
```

Each of the printed results reflects a single function call that the model has requested. To send the results back, include the responses in the same order as they were requested.

The Python SDK supports a feature called [automatic function calling](https://ai.google.dev/gemini-api/docs/function-calling#automatic_function_calling_python_only) which converts the Python function to declarations, handles the function call execution and response cycle for you. Following is an example for our disco use case.

**Note:** Automatic Function Calling is a Python SDK only feature at the moment.


```
from google import genai
from google.genai import types

# Actual implementation functions
def power_disco_ball_impl(power: bool) -> dict:
    """Powers the spinning disco ball.

    Args:
        power: Whether to turn the disco ball on or off.

    Returns:
        A status dictionary indicating the current state.
    """
    return {"status": f"Disco ball powered {'on' if power else 'off'}"}

def start_music_impl(energetic: bool, loud: bool) -> dict:
    """Play some music matching the specified parameters.

    Args:
        energetic: Whether the music is energetic or not.
        loud: Whether the music is loud or not.

    Returns:
        A dictionary containing the music settings.
    """
    music_type = "energetic" if energetic else "chill"
    volume = "loud" if loud else "quiet"
    return {"music_type": music_type, "volume": volume}

def dim_lights_impl(brightness: float) -> dict:
    """Dim the lights.

    Args:
        brightness: The brightness of the lights, 0.0 is off, 1.0 is full.

    Returns:
        A dictionary containing the new brightness setting.
    """
    return {"brightness": brightness}

config = {
    "tools": [power_disco_ball_impl, start_music_impl, dim_lights_impl],
}

chat = client.chats.create(model="gemini-2.0-flash", config=config)
response = chat.send_message("Do everything you need to this place into party!")

print("\nExample 2: Automatic function calling")
print(response.text)
# I've turned on the disco ball, started playing loud and energetic music, and dimmed the lights to 50% brightness. Let's get this party started!
```

## Compositional Function Calling

Compositional or sequential function calling allows Gemini to chain multiple function calls together to fulfill a complex request. For example, to answer "Get the temperature in my current location", the Gemini API might first invoke a `get_current_location()` function followed by a `get_weather()` function that takes the location as a parameter.

The following example demonstrates how to implement compositional function calling using the Python SDK and automatic function calling.

This example uses the automatic function calling feature of the `google-genai` Python SDK. The SDK automatically converts the Python functions to the required schema, executes the function calls when requested by the model, and sends the results back to the model to complete the task.

```
import os
from google import genai
from google.genai import types

# Example Functions
def get_weather_forecast(location: str) -> dict:
    """Gets the current weather temperature for a given location."""
    print(f"Tool Call: get_weather_forecast(location={location})")
    # TODO: Make API call
    print("Tool Response: {'temperature': 25, 'unit': 'celsius'}")
    return {"temperature": 25, "unit": "celsius"}  # Dummy response

def set_thermostat_temperature(temperature: int) -> dict:
    """Sets the thermostat to a desired temperature."""
    print(f"Tool Call: set_thermostat_temperature(temperature={temperature})")
    # TODO: Interact with a thermostat API
    print("Tool Response: {'status': 'success'}")
    return {"status": "success"}

# Configure the client and model
client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)  # Replace with your actual API key setup
config = types.GenerateContentConfig(
    tools=[get_weather_forecast, set_thermostat_temperature]
)

# Make the request
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="If it's warmer than 20°C in London, set the thermostat to 20°C, otherwise set it to 18°C.",
    config=config,
)

# Print the final, user-facing response
print(response.text)
```

**Expected Output**

When you run the code, you will see the SDK orchestrating the function calls. The model first calls `get_weather_forecast`, receives the temperature, and then calls `set_thermostat_temperature` with the correct value based on the logic in the prompt.

```
Tool Call: get_weather_forecast(location=London)
Tool Response: {'temperature': 25, 'unit': 'celsius'}
Tool Call: set_thermostat_temperature(temperature=20)
Tool Response: {'status': 'success'}
OK. I've set the thermostat to 20°C.
```

This example shows how to use JavaScript/TypeScript SDK to do comopositional function calling using a manual execution loop.

```
import { GoogleGenAI, Type } from "@google/genai";

// Configure the client
const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });

// Example Functions
function get_weather_forecast({ location }) {
  console.log(`Tool Call: get_weather_forecast(location=${location})`);
  // TODO: Make API call
  console.log("Tool Response: {'temperature': 25, 'unit': 'celsius'}");
  return { temperature: 25, unit: "celsius" };
}

function set_thermostat_temperature({ temperature }) {
  console.log(
    `Tool Call: set_thermostat_temperature(temperature=${temperature})`,
  );
  // TODO: Make API call
  console.log("Tool Response: {'status': 'success'}");
  return { status: "success" };
}

const toolFunctions = {
  get_weather_forecast,
  set_thermostat_temperature,
};

const tools = [
  {
    functionDeclarations: [
      {
        name: "get_weather_forecast",
        description:
          "Gets the current weather temperature for a given location.",
        parameters: {
          type: Type.OBJECT,
          properties: {
            location: {
              type: Type.STRING,
            },
          },
          required: ["location"],
        },
      },
      {
        name: "set_thermostat_temperature",
        description: "Sets the thermostat to a desired temperature.",
        parameters: {
          type: Type.OBJECT,
          properties: {
            temperature: {
              type: Type.NUMBER,
            },
          },
          required: ["temperature"],
        },
      },
    ],
  },
];

// Prompt for the model
let contents = [
  {
    role: "user",
    parts: [
      {
        text: "If it's warmer than 20°C in London, set the thermostat to 20°C, otherwise set it to 18°C.",
      },
    ],
  },
];

// Loop until the model has no more function calls to make
while (true) {
  const result = await ai.models.generateContent({
    model: "gemini-2.0-flash",
    contents,
    config: { tools },
  });

  if (result.functionCalls && result.functionCalls.length > 0) {
    const functionCall = result.functionCalls[0];

    const { name, args } = functionCall;

    if (!toolFunctions[name]) {
      throw new Error(`Unknown function call: ${name}`);
    }

    // Call the function and get the response.
    const toolResponse = toolFunctions[name](args);

    const functionResponsePart = {
      name: functionCall.name,
      response: {
        result: toolResponse,
      },
    };

    // Send the function response back to the model.
    contents.push({
      role: "model",
      parts: [
        {
          functionCall: functionCall,
        },
      ],
    });
    contents.push({
      role: "user",
      parts: [
        {
          functionResponse: functionResponsePart,
        },
      ],
    });
  } else {
    // No more function calls, break the loop.
    console.log(result.text);
    break;
  }
}
```

**Expected Output**

When you run the code, you will see the SDK orchestrating the function calls. The model first calls `get_weather_forecast`, receives the temperature, and then calls `set_thermostat_temperature` with the correct value based on the logic in the prompt.

```
Tool Call: get_weather_forecast(location=London)
Tool Response: {'temperature': 25, 'unit': 'celsius'}
Tool Call: set_thermostat_temperature(temperature=20)
Tool Response: {'status': 'success'}
OK. It's 25°C in London, so I've set the thermostat to 20°C.
```

Compositional function calling is a native [Live API](https://ai.google.dev/gemini-api/docs/live) feature. This means Live API can handle the function calling similar to the Python SDK.

```
# Light control schemas
turn_on_the_lights_schema = {'name': 'turn_on_the_lights'}
turn_off_the_lights_schema = {'name': 'turn_off_the_lights'}

prompt = """
  Hey, can you write run some python code to turn on the lights, wait 10s and then turn off the lights?
  """

tools = [
    {'code_execution': {}},
    {'function_declarations': [turn_on_the_lights_schema, turn_off_the_lights_schema]}
]

await run(prompt, tools=tools, modality="AUDIO")
```

## Function calling modes

The Gemini API lets you control how the model uses the provided tools (function declarations). Specifically, you can set the mode within the `function_calling_config`.

- `AUTO (Default)`: The model decides whether to generate a natural language response or suggest a function call based on the prompt and context. This is the most flexible mode and recommended for most scenarios.
- `ANY`: The model is constrained to always predict a function call and guarantee function schema adherence. If `allowed_function_names` is not specified, the model can choose from any of the provided function declarations. If `allowed_function_names` is provided as a list, the model can only choose from the functions in that list. Use this mode when you require a function call in response to every prompt (if applicable).
- `NONE`: The model is _prohibited_ from making function calls. This is equivalent to sending a request without any function declarations. Use this to temporarily disable function calling without removing your tool definitions.
    

```
from google.genai import types

# Configure function calling mode
tool_config = types.ToolConfig(
    function_calling_config=types.FunctionCallingConfig(
        mode="ANY", allowed_function_names=["get_current_temperature"]
    )
)

# Create the generation config
config = types.GenerateContentConfig(
    temperature=0,
    tools=[tools],  # not defined here.
    tool_config=tool_config,
)
```

## Automatic Function Calling (Python Only)

When using the Python SDK, you can provide Python functions directly as tools. The SDK automatically converts the Python function to declarations, handles the function call execution and response cycle for you. The Python SDK then automatically:

1. Detects function call responses from the model.
2. Call the corresponding Python function in your code.
3. Sends the function response back to the model.
4. Returns the model's final text response.

To use this, define your function with type hints and a docstring, and then pass the function itself (not a JSON declaration) as a tool:

```
from google import genai
from google.genai import types

# Define the function with type hints and docstring
def get_current_temperature(location: str) -> dict:
    """Gets the current temperature for a given location.

    Args:
        location: The city and state, e.g. San Francisco, CA

    Returns:
        A dictionary containing the temperature and unit.
    """
    # ... (implementation) ...
    return {"temperature": 25, "unit": "Celsius"}

# Configure the client and model
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))  # Replace with your actual API key setup
config = types.GenerateContentConfig(
    tools=[get_current_temperature]
)  # Pass the function itself

# Make the request
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="What's the temperature in Boston?",
    config=config,
)

print(response.text)  # The SDK handles the function call and returns the final text
```

You can disable automatic function calling with:


```
# To disable automatic function calling:
config = types.GenerateContentConfig(
    tools=[get_current_temperature],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
)
```

### Automatic Function schema declaration

Automatic schema extraction from Python functions doesn't work in all cases. For example: it doesn't handle cases where you describe the fields of a nested dictionary-object. The API is able to describe any of the following types:


```
AllowedType = (int | float | bool | str | list['AllowedType'] | dict[str, AllowedType])
```

To see what the inferred schema looks like, you can convert it using [`from_callable`](https://googleapis.github.io/python-genai/genai.html#genai.types.FunctionDeclaration.from_callable):


```
def multiply(a: float, b: float):
    """Returns a * b."""
    return a * b

fn_decl = types.FunctionDeclaration.from_callable(callable=multiply, client=client)

# to_json_dict() provides a clean JSON representation.
print(fn_decl.to_json_dict())
```

## Multi-tool use: Combine Native Tools with Function Calling

With Gemini 2.0, you can enable multiple tools combining native tools with function calling at the same time. Here's an example that enables two tools, [Grounding with Google Search](https://ai.google.dev/gemini-api/docs/grounding) and [code execution](https://ai.google.dev/gemini-api/docs/code-execution), in a request using the [Live API](https://ai.google.dev/gemini-api/docs/live).

**Note:** Multi-tool use is a [Live API](https://ai.google.dev/gemini-api/docs/live) only feature at the moment. The `run()` function declaration, which handles the asynchronous websocket setup, is omitted for brevity.


```
# Multiple tasks example - combining lights, code execution, and search
prompt = """
  Hey, I need you to do three things for me.

    1.  Turn on the lights.
    2.  Then compute the largest prime palindrome under 100000.
    3.  Then use Google Search to look up information about the largest earthquake in California the week of Dec 5 2024.

  Thanks!
  """

tools = [
    {'google_search': {}},
    {'code_execution': {}},
    {'function_declarations': [turn_on_the_lights_schema, turn_off_the_lights_schema]} # not defined here.
]

# Execute the prompt with specified tools in audio modality
await run(prompt, tools=tools, modality="AUDIO")
```

Python developers can try this out in the [Live API Tool Use notebook](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI_tools.ipynb).

## Model Context Protocol (MCP)

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/introduction) is an open standard for connecting AI applications with external tools and data. MCP provides a common protocol for models to access context, such as functions (tools), data sources (resources), or predefined prompts.

The Gemini SDKs have built-in support for MCP, reducing boilerplate code and offering [automatic tool calling](https://ai.google.dev/gemini-api/docs/function-calling#automatic_function_calling_python_only) for MCP tools. When the model generates an MCP tool call, the Python and JavaScript client SDK can automatically execute the MCP tool and send the response back to the model in a subsequent request, continuing this loop until no more tool calls are made by the model.

Here, you can find an example of how to use a local MCP server with Gemini and `mcp` SDK.

Make sure the latest version of the [`mcp` SDK](https://modelcontextprotocol.io/introduction) is installed on your platform of choice.

```
pip install mcp
```

**Note:** Python supports automatic tool calling by passing in the `ClientSession` into the `tools` parameters. If you want to disable it you can provide `automatic_function_calling` with disabled `True`.

```
import os
import asyncio
from datetime import datetime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from google import genai

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Create server parameters for stdio connection
server_params = StdioServerParameters(
    command="npx",  # Executable
    args=["-y", "@philschmid/weather-mcp"],  # MCP Server
    env=None,  # Optional environment variables
)

async def run():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Prompt to get the weather for the current day in London.
            prompt = f"What is the weather in London in {datetime.now().strftime('%Y-%m-%d')}?"
            # Initialize the connection between client and server
            await session.initialize()
            # Send request to the model with MCP function declarations
            response = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    temperature=0,
                    tools=[session],  # uses the session, will automatically call the tool
                    # Uncomment if you **don't** want the sdk to automatically call the tool
                    # automatic_function_calling=genai.types.AutomaticFunctionCallingConfig(
                    #     disable=True
                    # ),
                ),
            )
            print(response.text)

# Start the asyncio event loop and run the main function
asyncio.run(run())
```

Make sure the latest version of the `mcp` SDK is installed on your platform of choice.

```
npm install @modelcontextprotocol/sdk
```

**Note:** JavaScript supports automatic tool calling by wrapping the `client` with `mcpToTool`. If you want to disable it you can provide `automaticFunctionCalling` with disabled `true`.

### Limitations with built-in MCP Support

Built-in MCP support is a [experimental](https://ai.google.dev/gemini-api/docs/models#preview) feature in our SDKs and has the following limitations:

- Only tools are supported, not resources or prompts
- It is available for the Python and JavaScript/TypeScript SDK.
- Breaking changes might occur in future releases.

Manual integration of MCP servers is always an option if these limit what you're building.

## Supported Models

Experimental models are not included. You can find their capabilities on the [model overview](https://ai.google.dev/gemini-api/docs/models) page.

| Model | Function Calling | Parallel Function Calling | Compositional Function Calling |
| --- | --- | --- | --- |
| Gemini 2.0 Flash | ✔️ | ✔️ | ✔️ |
| Gemini 2.0 Flash-Lite | X | X | X |
| Gemini 1.5 Flash | ✔️ | ✔️ | ✔️ |
| Gemini 1.5 Pro | ✔️ | ✔️ | ✔️ |

## Best Practices

- **Function and Parameter Descriptions:** Be extremely clear and specific in your descriptions. The model relies on these to choose the correct function and provide appropriate arguments.
- **Naming:** Use descriptive function names (without spaces, periods, or dashes).
- **Strong Typing:** Use specific types (integer, string, enum) for parameters to reduce errors. If a parameter has a limited set of valid values, use an enum.
- **Tool Selection:** While the model can use an arbitrary number of tools, providing too many can increase the risk of selecting an incorrect or suboptimal tool. For best results, aim to provide only the relevant tools for the context or task, ideally keeping the active set to a maximum of 10-20. Consider dynamic tool selection based on conversation context if you have a large total number of tools.
- **Prompt Engineering:**
    - Provide context: Tell the model its role (e.g., "You are a helpful weather assistant.").
    - Give instructions: Specify how and when to use functions (e.g., "Don't guess dates; always use a future date for forecasts.").
    - Encourage clarification: Instruct the model to ask clarifying questions if needed.
- **Temperature:** Use a low temperature (e.g., 0) for more deterministic and reliable function calls.
- **Validation:** If a function call has significant consequences (e.g., placing an order), validate the call with the user before executing it.
- **Error Handling**: Implement robust error handling in your functions to gracefully handle unexpected inputs or API failures. Return informative error messages that the model can use to generate helpful responses to the user.
- **Security:** Be mindful of security when calling external APIs. Use appropriate authentication and authorization mechanisms. Avoid exposing sensitive data in function calls.
- **Token Limits:** Function descriptions and parameters count towards your input token limit. If you're hitting token limits, consider limiting the number of functions or the length of the descriptions, break down complex tasks into smaller, more focused function sets.

## Notes and Limitations

- Only a [subset of the OpenAPI schema](https://ai.google.dev/api/caching#FunctionDeclaration) is supported.
- Supported parameter types in Python are limited.
- Automatic function calling is a Python SDK feature only.