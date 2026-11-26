"""Strategy plug-ins. Add a module here and register it in REGISTRY (name must match config.STRATEGIES)."""
from strategies.strangle60 import Strangle60
from strategies.otm4_1day import Otm4OneDay
from strategies.btst9 import Btst9
from strategies.st10 import St10

REGISTRY = {
    "strangle60": Strangle60,
    "otm4_1day": Otm4OneDay,
    "btst9": Btst9,
    "st10": St10,
}
