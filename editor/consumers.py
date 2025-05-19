import json
import docker
import base64
import asyncio
import socket
from channels.generic.websocket import AsyncWebsocketConsumer
import logging
from docker.errors import APIError
import re

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("WebSocket connection attempt")
        try:
            self.client = docker.from_env()
            self.container = None
            self.exec_id = None
            self.exec_socket = None
            self.input_queue = asyncio.Queue()
            self.is_running = False
            self.current_code = None
            self.input_count = 0
            self.input_processed = 0
            await self.accept()
            logger.info("WebSocket connected")
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Connection failed: {str(e)}'}))
            await self.close(code=3000)

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code: {close_code}")
        await self.cleanup_container()
        self.is_running = False

    async def receive(self, text_data):
        logger.info(f"Received WebSocket message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'code' in data:
                # Reuse container if same code and waiting for input
                if (self.current_code == data['code'] and self.is_running and 
                    self.exec_id and self.input_queue.empty()):
                    logger.info("Same code received, continuing with existing container")
                    await self.send(text_data=json.dumps({'output': 'Waiting for input >>> '}))
                else:
                    if self.is_running:
                        await self.cleanup_container()
                    await self.execute_code(data['code'])
            elif 'input' in data:
                if self.is_running and self.exec_id:
                    await self.input_queue.put(data['input'])
                    self.input_processed += 1
                else:
                    await self.send(text_data=json.dumps({'error': 'No active container for input'}))
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
            # Create container
            self.container = self.client.containers.create(
                'python:3.12-slim',
                command='tail -f /dev/null',
                tty=False,
                stdin_open=True
            )
            self.container.start()
            logger.info("Container started")

            # Write code to /app/code.py
            encoded_code = base64.b64encode(code.encode()).decode()
            exec_result = self.container.exec_run(
                ['sh', '-c', f'mkdir -p /app && echo "{encoded_code}" | base64 -d > /app/code.py'],
                stdout=True,
                stderr=True
            )
            if exec_result.exit_code != 0:
                error_msg = exec_result.output.decode(errors='ignore')
                logger.error(f"Failed to write code file: {error_msg}")
                await self.send(text_data=json.dumps({'error': f'Failed to write code: {error_msg}'}))
                await self.cleanup_container()
                return

            # Start interactive Python process
            self.exec_id = self.client.api.exec_create(
                self.container.id,
                ['python', '-u', '/app/code.py'],
                stdout=True,
                stderr=True,
                stdin=True,
                tty=False
            )['Id']

            # Start streaming with socket
            self.exec_socket = self.client.api.exec_start(
                self.exec_id,
                stream=True,
                socket=True,
                detach=False
            )
            self.exec_socket._sock.settimeout(60)
            self.is_running = True

            # Start tasks
            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())

        except APIError as e:
            logger.error(f"Failed to start container or execution: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_container()
        except Exception as e:
            logger.error(f"Unexpected error in execute_code: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Unexpected error: {str(e)}'}))
            await self.cleanup_container()

    async def stream_output(self):
        try:
            while self.is_running:
                try:
                    # Check process status
                    if self.exec_id:
                        inspect = self.client.api.exec_inspect(self.exec_id)
                        if not inspect['Running']:
                            logger.info("Python process exited")
                            break

                    # Read from socket
                    chunk = await asyncio.get_event_loop().run_in_executor(
                        None, self.exec_socket._sock.recv, 4096
                    )
                    if not chunk:
                        if self.current_code and self.input_count > self.input_processed:
                            await asyncio.sleep(0.1)
                            continue
                        logger.info("Execution stream closed")
                        break
                    output = chunk.decode(errors='ignore')
                    output = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', output)
                    if output:
                        # Add prompt only for input() prompts
                        formatted_output = output
                        if self.input_count > self.input_processed:
                            formatted_output = output.rstrip() + ' >>> '
                        # Handle common errors
                        if 'Traceback' in output:
                            if 'SyntaxError' in output:
                                formatted_output = 'Error: Check for missing colons, parentheses, or indentation.'
                            elif 'NameError' in output:
                                formatted_output = 'Error: You used a variable that wasn’t defined.'
                            self.input_count = 0
                            self.input_processed = 0
                        logger.info(f"Sending output: {formatted_output}")
                        await self.send(text_data=json.dumps({'output': formatted_output}))
                except socket.timeout:
                    logger.warning("Socket timeout in stream_output, continuing")
                    continue
                except Exception as e:
                    logger.error(f"Error streaming output: {str(e)}")
                    await self.send(text_data=json.dumps({'error': f'Streaming error: {str(e)}'}))
                    break
        finally:
            if self.input_queue.empty() and self.exec_id:
                inspect = self.client.api.exec_inspect(self.exec_id)
                if not inspect['Running']:
                    await self.cleanup_container()

    async def process_input(self):
        try:
            while self.is_running:
                input_data = await self.input_queue.get()
                if not input_data.endswith('\n'):
                    input_data += '\n'
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self.exec_socket._sock.send, input_data.encode()
                    )
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
            if self.input_queue.empty() and self.exec_id:
                inspect = self.client.api.exec_inspect(self.exec_id)
                if not inspect['Running']:
                    await self.cleanup_container()

    async def cleanup_container(self):
        if self.exec_socket:
            try:
                self.exec_socket._sock.settimeout(None)
                self.exec_socket.close()
            except Exception as e:
                logger.error(f"Error closing exec socket: {str(e)}")
            self.exec_socket = None
        if self.container:
            try:
                self.container.stop(timeout=1)
                self.container.remove()
                logger.info("Container cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up container: {str(e)}")
            self.container = None
        self.exec_id = None
        self.is_running = False
        self.current_code = None
        self.input_count = 0
        self.input_processed = 0