import json
import asyncio
import subprocess
import tempfile
import re
from channels.generic.websocket import AsyncWebsocketConsumer
import logging
import socket

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("WebSocket connection attempt")
        try:
            # Initialize attributes
            self.process = None
            self.input_queue = asyncio.Queue()
            self.is_running = False
            self.current_code = None
            self.input_count = 0
            self.input_processed = 0
            await self.accept()
            logger.info("WebSocket connected")
            await self.send(text_data=json.dumps({'message': 'WebSocket connection established'}))
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            await self.close(code=3000, reason=f'Connection failed: {str(e)}')

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code: {close_code}")
        await self.cleanup_process()
        self.is_running = False

    async def receive(self, text_data):
        logger.info(f"Received WebSocket message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'code' in data:
                if (self.current_code == data['code'] and self.is_running and 
                    self.process and self.input_queue.empty()):
                    logger.info("Same code received, continuing with existing process")
                    await self.send(text_data=json.dumps({'output': 'Waiting for input >>> '}))
                else:
                    if self.is_running:
                        await self.cleanup_process()
                    await self.execute_code(data['code'])
            elif 'input' in data:
                if self.is_running and self.process:
                    await self.input_queue.put(data['input'])
                    self.input_processed += 1
                else:
                    await self.send(text_data=json.dumps({'error': 'No active process for input'}))
            else:
                await self.send(text_data=json.dumps({'error': 'Invalid message format. Use {"code": "..."} or {"input": "..."}'}))
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Invalid message: {str(e)}'}))
            await self.close(code=3000)
        except Exception as e:
            logger.error(f"Unexpected error in receive: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Unexpected error: {str(e)}'}))

    async def execute_code(self, code):
        logger.info("Executing code")
        self.current_code = code
        self.input_count = len(re.findall(r'\binput\s*\(', code))
        self.input_processed = 0
        try:
            with tempfile.NamedTemporaryFile(suffix='.py', dir='/tmp', delete=False) as temp_file:
                temp_file.write(code.encode())
                temp_file.flush()
                self.process = await asyncio.create_subprocess_exec(
                    'python', '-u', temp_file.name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE
                )
            self.is_running = True
            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())
        except Exception as e:
            logger.error(f"Execution error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running:
                try:
                    # Check process status
                    if self.process.returncode is not None:
                        logger.info("Python process exited")
                        break

                    # Read stdout
                    stdout = await asyncio.wait_for(self.process.stdout.readline(), timeout=60)
                    if not stdout:
                        if self.current_code and self.input_count > self.input_processed:
                            await asyncio.sleep(0.1)
                            continue
                        logger.info("Execution stream closed")
                        break
                    output = stdout.decode(errors='ignore')
                    output = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', output)
                    if output:
                        formatted_output = output
                        if self.input_count > self.input_processed:
                            formatted_output = output.rstrip() + ' >>> '
                        if 'Traceback' in output:
                            if 'SyntaxError' in output:
                                formatted_output = 'Error: Check for missing colons, parentheses, or indentation.'
                            elif 'NameError' in output:
                                formatted_output = 'Error: You used a variable that wasn’t defined.'
                            self.input_count = 0
                            self.input_processed = 0
                        logger.info(f"Sending output: {formatted_output}")
                        await self.send(text_data=json.dumps({'output': formatted_output}))

                    # Read stderr
                    try:
                        stderr = await asyncio.wait_for(self.process.stderr.readline(), timeout=0.1)
                        if stderr:
                            output = stderr.decode(errors='ignore')
                            output = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', output)
                            if output:
                                logger.info(f"Sending error output: {output}")
                                await self.send(text_data=json.dumps({'output': output}))
                    except asyncio.TimeoutError:
                        pass
                except asyncio.TimeoutError:
                    logger.warning("Timeout in stream_output, continuing")
                    continue
                except Exception as e:
                    logger.error(f"Error streaming output: {str(e)}")
                    await self.send(text_data=json.dumps({'error': f'Streaming error: {str(e)}'}))
                    break
        finally:
            if self.input_queue.empty() and self.process and self.process.returncode is not None:
                await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running:
                input_data = await self.input_queue.get()
                if not input_data.endswith('\n'):
                    input_data += '\n'
                try:
                    await self.process.stdin.write(input_data.encode())
                    await self.process.stdin.drain()
                    logger.info(f"Sent input: {input_data.strip()}")
                except Exception as e:
                    logger.error(f"Error sending input: {str(e)}")
                    await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
                finally:
                    self.input_queue.task_done()
        except Exception as e:
            logger.error(f"Error in process_input: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input processing error: {str(e)}'}))
        finally:
            if self.input_queue.empty() and self.process and self.process.returncode is not None:
                await self.cleanup_process()

    async def cleanup_process(self):
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=1)
            except Exception as e:
                logger.error(f"Error cleaning up process: {str(e)}")
            self.process = None
        self.is_running = False
        self.current_code = None
        self.input_count = 0
        self.input_processed = 0
