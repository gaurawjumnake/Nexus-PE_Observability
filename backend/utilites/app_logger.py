import sys
import inspect
from loguru import logger
from collections import deque
import threading


class Logger:
    _configured = False

    def __init__(self) -> None:
        self.log_buffer = deque(maxlen=1000) 
        self.buffer_lock = threading.Lock()
        if not Logger._configured:
            self.setup_logger()
            Logger._configured = True

    def custom_sink(self, message):
        """Custom sink to capture logs in buffer"""
        with self.buffer_lock:
            self.log_buffer.append(message)

    def setup_logger(self):
        """
        Setup loguru logger with colored output and module/method information
        """
        # Remove default logger
        logger.remove()
        
        # Add colored console logger with custom format
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{extra[module]}</cyan>:<yellow>{extra[method]}</yellow> | "
                "<level>{message}</level>",
            level="DEBUG",
            colorize=True
        )
        
        # Optional: Add file logger (uncomment if needed)
        # logger.add(
        #     "app.log",
        #     format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]}:{extra[method]} | {message}",
        #     level="DEBUG",
        #     rotation="10 MB"
        # )

        logger.add(
            self.custom_sink,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]}:{extra[method]} | {message}",
            level="DEBUG"
        )
        
    def get_recent_logs(self, count=None):
            """Get recent logs from buffer"""
            with self.buffer_lock:
                if count:
                    return list(self.log_buffer)[-count:]
                return list(self.log_buffer)

    def get_caller_info(self, level: int = 3):
        """
        Get caller module and function name.
        level=2 means: caller of caller (skip this function + direct caller).
        """
        stack = inspect.stack()
        if len(stack) <= level:
            return "unknown", "unknown"
        
        frame_info = stack[level]
        module = inspect.getmodule(frame_info.frame)
        module_name = module.__name__ if module else "unknown"
        method_name = frame_info.function
        return module_name, method_name

    def log_debug(self, message):
        """Log debug message with caller info"""
        module, method = self.get_caller_info()
        logger.bind(module=module, method=method).debug(message)

    def log_info(self, message):
        """Log info message with caller info"""
        module, method = self.get_caller_info()
        logger.bind(module=module, method=method).info(message)

    def log_warning(self, message):
        """Log warning message with caller info"""
        module, method = self.get_caller_info()
        logger.bind(module=module, method=method).warning(message)

    def log_error(self, message):
        """Log error message with caller info"""
        module, method = self.get_caller_info()
        logger.bind(module=module, method=method).error(message)

    def log_critical(self, message):
        """Log critical message with caller info"""
        module, method = self.get_caller_info()
        logger.bind(module=module, method=method).critical(message)


# log = Logger()

# # Example usage
# if __name__ == "__main__":
#     def example_function():
#         log.log_debug("This is a debug message")
#         log.log_info("Application started successfully")
#         log.log_warning("This is a warning message")
#         log.log_error("An error occurred while processing")
#         log.log_critical("Critical system failure!")
    
#     def another_function():
#         log.log_info("Processing data in another function")
#         log.log_warning("Low memory warning")
    
#     # Test the logging
#     example_function()
#     another_function()
    
#     # You can also use the logger directly with context
#     module, method = log.get_caller_info()
#     logger.bind(module=module, method=method).success("Operation completed successfully!")