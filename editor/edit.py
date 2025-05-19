import json
import docker
import base64
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
import logging
from docker.errors import APIError

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("WebSocket connection attempt")
        try:
            self.client = docker.from_env()
            self.current_code = None
            self.container = None
            self.input_queue = []
            self.is_processing = False
            await self.accept()
            logger.info("WebSocket connected")
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Connection failed: {str(e)}'}))
            await self.close(code=3000)

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code: {close_code}")
        if self.container:
            try:
                self.container.stop()
                self.container.remove()
                logger.info("Container cleaned up")
            except Exception as e:
                logger.error(f"Error stopping container: {str(e)}")
            self.container = None

    async def receive(self, text_data):
        logger.info(f"Received WebSocket message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'code' in data:
                self.current_code = data['code']
                await self.execute_code(data['code'])
            elif 'input' in data:
                self.input_queue.append(data['input'])
                await self.process_input()
            else:
                await self.send(text_data=json.dumps({'error': 'Invalid message format'}))
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Invalid message: {str(e)}'}))
            await self.close(code=3000)
        except Exception as e:
            logger.error(f"Unexpected error in receive: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Unexpected error: {str(e)}'}))

    async def execute_code(self, code):
        logger.info("Executing code")
        if self.container:
            try:
                self.container.stop()
                self.container.remove()
                logger.info("Previous container cleaned up")
            except Exception as e:
                logger.error(f"Error removing old container: {str(e)}")

        try:
            self.container = self.client.containers.create(
                'python:3.12-slim',
                command='tail -f /dev/null',
                tty=True,
                stdin_open=True
            )
            self.container.start()
            logger.info("Container started")
        except APIError as e:
            logger.error(f"Failed to start container: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            return
        except Exception as e:
            logger.error(f"Unexpected error creating container: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Unexpected error: {str(e)}'}))
            return

        try:
            encoded_code = base64.b64encode(code.encode()).decode()
            exec_result = self.container.exec_run(
                ['sh', '-c', f'mkdir -p /app && echo "{encoded_code}" | base64 -d > /app/code.py'],
                stdout=True,
                stderr=True
            )
            if exec_result.exit_code != 0:
                logger.error(f"Failed to write code file: {exec_result.output.decode(errors='ignore')}")
                await self.send(text_data=json.dumps({'error': f'Failed to write code: {exec_result.output.decode(errors="ignore")}'}))
                return

            exec_result = self.container.exec_run(
                ['python', '-u', '/app/code.py'],
                stdout=True,
                stderr=True
            )
            output = exec_result.output.decode(errors='ignore')
            if 'Traceback' in output and 'EOFError' in output:
                output = output.split('Traceback')[0] + 'Error: Waiting for input. Please provide input in the terminal.'
                await self.send(text_data=json.dumps({'output': output}))
            elif 'Traceback' in output:
                if 'SyntaxError' in output:
                    output = 'Error: Check for missing colons, parentheses, or indentation.'
                elif 'NameError' in output:
                    output = 'Error: You used a variable that wasn’t defined.'
                await self.send(text_data=json.dumps({'output': output}))
            elif output:
                await self.send(text_data=json.dumps({'output': output}))
            else:
                logger.warning("No output from execution")
        except Exception as e:
            logger.error(f"Error executing code: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution error: {str(e)}'}))
            return

    async def process_input(self):
        if self.is_processing or not self.input_queue or not self.container or not self.current_code:
            logger.warning("Cannot process input: missing container or code")
            await self.send(text_data=json.dumps({'error': 'No active code execution for input'}))
            return
        self.is_processing = True
        input_data = self.input_queue.pop(0)
        try:
            encoded_input = base64.b64encode(input_data.encode()).decode()
            exec_result = self.container.exec_run(
                ['sh', '-c', f'echo "{encoded_input}" | base64 -d | python -u /app/code.py'],
                stdout=True,
                stderr=True
            )
            output = exec_result.output.decode(errors='ignore')
            if output:
                await self.send(text_data=json.dumps({'output': output}))
            else:
                logger.warning("No output from input execution")
        except Exception as e:
            logger.error(f"Error processing input: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
        finally:
            self.is_processing = False
            if not self.input_queue:
                try:
                    self.container.exec_run(['rm', '/app/code.py'])
                    self.container.stop()
                    self.container.remove()
                    logger.info("Container cleaned up after input")
                    self.container = None
                    self.current_code = None
                except Exception as e:
                    logger.error(f"Error cleaning up container: {str(e)}")
