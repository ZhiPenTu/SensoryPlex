"""旧元数据导入路径；复用唯一实现，不维护第二套事务语义。"""

from sensoryplex_api.infrastructure.materials import (
    RevisionConflict as RevisionConflict,
)
from sensoryplex_api.infrastructure.materials import (
    append_material as append_material,
)
from sensoryplex_api.infrastructure.materials import (
    get_material as get_material,
)
from sensoryplex_api.infrastructure.materials import (
    search_materials as search_materials,
)
