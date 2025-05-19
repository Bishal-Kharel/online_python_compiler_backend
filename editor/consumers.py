import json
import asyncio
import subprocess
import tempfile
import re
from channels.generic.websocket import AsyncWebsocketConsumer
import logging

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("WebSocket connection attempt")
        try:
            self.process = None
            self.input_queue = asyncio.Queue()
            self.is_running = False
            self.current_code = None
            self.input_count = 0
            self.input_processed = 0
            self._output_task = None
            self._input_task = None
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
            if 'ping' in data:
                return  # Ignore ping messages
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
                if not isinstance(data['input'], str) or len(data['input']) > 1000:
                    await self.send(text_data=json.dumps({'error': 'Invalid input'}))
                    return
                if self.is_running and self.process and self.process.stdin:
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
                    stdin=asyncio.subprocess.PIPE,
                    close_fds=True
                )
            self.is_running = True
            self._output_task = asyncio.create_task(self.stream_output())
            self._input_task = asyncio.create_task(self.process_input())
        except Exception as e:
            logger.error(f"Execution error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                try:
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
            logger.info("Python process exited")
        finally:
            if self.input_queue.empty() or self.input_processed >= self.input_count:
                await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                input_data = await self.input_queue.get()
                try:
                    if self.process.stdin and not self.process.stdin.is_closing():
                        self.process.stdin.write(input_data.encode() + b'\n')
                        await self.process.stdin.drain()
                        logger.info(f"Sent input: {input_data.strip()}")
                    else:
                        logger.error("Stdin is closed or unavailable")
                        await self.send(text_data=json.dumps({'error': 'Cannot send input: stdin unavailable'}))
                        break
                except Exception as e:
                    logger.error(f"Error sending input: {str(e)}")
                    await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
                    break
                finally:
                    self.input_queue.task_done()
            logger.info("Input processing stopped")
        except asyncio.CancelledError:
            logger.info("Input processing task cancelled")
        except Exception as e:
            logger.error(f"Error in process_input: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input processing error: {str(e)}'}))
        finally:
            if self.input_queue.empty() or self.input_processed >= self.input_count:
                await self.cleanup_process()

    async def cleanup_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=2)
                logger.info("Process terminated")
            except Exception as e:
                logger.error(f"Error terminating process: {str(e)}")
        if self.process:
            try:
                if self.process.stdout:
                    await self.process.stdout.read()  # Drain remaining output
                if self.process.stderr:
                    await self.process.stderr.read()
                logger.info("Process streams drained")
            except Exception as e:
                logger.error(f"Error draining streams: {str(e)}")
            self.process = None
        if self._output_task:
            self._output_task.cancel()
            try:
                await self._output_task
            except asyncio.CancelledError:
                logger.info("Output task cancelled")
            self._output_task = None
        if self._input_task:
            self._input_task.cancel()
            try:
                await self._input_task
            except asyncio.CancelledError:
                logger.info("Input task cancelled")
            self._input_task = None
        self.is_running = False
        self.current_code = None
        self.input_count = 0
        self.input_processed = 0
        logger.info("Cleanup completed")
