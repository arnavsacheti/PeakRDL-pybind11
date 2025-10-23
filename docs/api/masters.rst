Master Backends
===============

.. automodule:: peakrdl_pybind11.masters
   :members:
   :undoc-members:
   :show-inheritance:
   :special-members: __init__

Usage Examples
--------------

Mock Master
~~~~~~~~~~~

For testing without hardware:

.. code-block:: python

   from peakrdl_pybind11.masters import MockMaster

   master = MockMaster()
   soc.attach_master(master)

OpenOCD Master
~~~~~~~~~~~~~~

For JTAG/SWD debugging:

.. code-block:: python

   from peakrdl_pybind11.masters import OpenOCDMaster

   master = OpenOCDMaster(host="localhost", port=4444)
   soc.attach_master(master)

SSH Master
~~~~~~~~~~

For remote access:

.. code-block:: python

   from peakrdl_pybind11.masters import SSHMaster

   master = SSHMaster(host="192.168.1.100", username="user", password="pass")
   soc.attach_master(master)

Custom Master Backend
~~~~~~~~~~~~~~~~~~~~~

You can implement custom master backends by inheriting from the base Master class:

.. code-block:: python

   from peakrdl_pybind11.masters import Master

   class MyCustomMaster(Master):
       def read(self, address, width):
           # Implement your read logic
           pass

       def write(self, address, width, value):
           # Implement your write logic
           pass
