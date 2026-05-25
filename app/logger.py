import logging
import sys

logger = logging.getLogger("pve-backup")
logger.setLevel(logging.DEBUG)

fmt = logging.Formatter(
    "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.DEBUG)
console.setFormatter(fmt)
logger.addHandler(console)
