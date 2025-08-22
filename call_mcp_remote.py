from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import AsyncExitStack
import json
import asyncio
from mcp import ClientSession, Tool
from mcp.client.sse import sse_client
from openai import OpenAI
import logging
from typing import Optional, Dict
from httpx import Timeout
import yaml
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI()

with open(Path(__file__).parent / "config.yaml") as f:
    config = yaml.safe_load(f)

class MCPConfig(BaseModel):
    mcpServers: Dict[str, Dict[str, str]]

class UserQuery(BaseModel):
    query: str
    mcp_config: MCPConfig

class MCPClient:
    def __init__(self, config: MCPConfig):
        self.config = config
        self.sessions: Dict[str, ClientSession] = {}
        self.settion_tools: Dict[str, list[Tool]] = {}
        self._exit_stacks: Dict[str, AsyncExitStack] = {}  # 新增

    async def connect_to_sse_servers(self):
        """Connect to all configured MCP servers"""
        for server_name, server_config in self.config.mcpServers.items():
            if server_config['type'] == 'sse':
                try:
                    exit_stack = AsyncExitStack()
                    streams = await exit_stack.enter_async_context(sse_client(url=server_config['url']))
                    session = await exit_stack.enter_async_context(ClientSession(*streams))
                    
                    await session.initialize()
                    self.sessions[server_name] = session
                    self._exit_stacks[server_name] = exit_stack  # 保存 exit_stack 以便后续清理
                        
                    logger.info(f"Connected to {server_name} at {server_config['url']}")
                        
                    response = await session.list_tools()
                    logger.info(f"Connected to {server_name} with tools: {[tool.name for tool in response.tools]}")
                        
                    self.settion_tools[server_name] = response.tools
                        
                except Exception as e:
                    logger.error(f"Failed to connect to {server_name}: {str(e)}")
                    continue

    async def cleanup(self):
        for server_name, exit_stack in self._exit_stacks.items():
            try:
                await exit_stack.aclose()  # 确保资源按顺序释放
            except Exception as e:
                logger.error(f"Cleanup error for {server_name}: {str(e)}")
        self.sessions.clear()
        self.settion_tools.clear()

    async def process_query(self, query: str) -> str:
        """Process query using all available MCP tools"""
        if not self.sessions:
            raise HTTPException(status_code=400, detail="No active MCP connections")
            
        messages = [{"role": "user", "content": query}]
        available_tools = []
        
        for server_idx, (server_name, tools) in enumerate(self.settion_tools.items()):  
            available_tools.extend([{
                "type": "function",
                "function": {
                    "name": f"server_{server_idx}_{tool.name}",  
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            } for tool in tools])

        
        model = config["openai"]["model"]
        base_url = config["openai"]["base_url"]
        api_key = config["openai"]["api_key"]
        timeout = config["openai"]["timeout"]
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout
        )
        logger.info(f"baseurl: {base_url}, model: {model}, timeout: {timeout}")
        try:
            logger.info(f"请求参数 - messages: {messages}, tools: {available_tools}")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=available_tools
            )
            logger.info(f"返回内容 - response: {response}")
        except Exception as e:
            logger.error(f"OpenAI API调用失败: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"OpenAI API调用失败: {str(e)}"
            )
        
        # Process response and handle tool calls
        tool_results = []
        final_text = []

        message = response.choices[0].message
        # final_text.append(message.content or "")

        while message and message.tool_calls:
            # Handle each tool call
            for tool_call in message.tool_calls:
                tool_args = json.loads(tool_call.function.arguments)
                # 根据tool_call.function.name，拆分出server_name和tool_name​
                # 使用正则表达式从函数名中提取server_index和tool_name
                import re
                match = re.match(r"server_(\d+)_(.+)", tool_call.function.name)
                if not match:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid tool call format: {tool_call.function.name}"
                    )
                server_index = match.group(1)
                tool_name = match.group(2)
                keylist=list(self.settion_tools.keys())
                server_name = keylist[int(server_index)]

                # Execute tool call
                logger.info(f"Calling tool {tool_name} with args {tool_args}")
                result = await self.sessions[server_name].call_tool(tool_name, tool_args)
                logger.info(f"Tool {tool_name} returned {result}")
                tool_results.append({"call": tool_name, "result": result})
                # final_text.append(f"[Calling tool {tool_name} with args {tool_args}]")

                # Add tool call and result to messages
                messages.append({
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args)
                            }
                        }
                    ]
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(result.content)
                })

            logger.info(f"\nllm req:{messages}")
            # Get next response from OpenAI
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=available_tools
            )
            
            message = response.choices[0].message
            if message.content:
                final_text.append(message.content)

        return "".join(final_text)

@app.post("/mcpcall")
async def handle_query(user_query: UserQuery):
    client = MCPClient(user_query.mcp_config)
    try:
        await client.connect_to_sse_servers()
        try:
            response = await asyncio.wait_for(
                client.process_query(user_query.query),
                timeout=300.0  # 设置300秒超时
            )
            return {"response": response}
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Request timeout")
    except Exception as e:
        logger.error(f"Error processing query: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.cleanup()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)