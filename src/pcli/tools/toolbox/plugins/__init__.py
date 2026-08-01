from pcli.tools.toolbox.plugins.httpd import HttpdPlugin
from pcli.tools.toolbox.plugins.kafka import KafkaPlugin
from pcli.tools.toolbox.plugins.kubectl import KubectlPlugin
from pcli.tools.toolbox.plugins.sge import SgePlugin

ALL_PLUGINS = [KubectlPlugin(), SgePlugin(), KafkaPlugin(), HttpdPlugin()]
