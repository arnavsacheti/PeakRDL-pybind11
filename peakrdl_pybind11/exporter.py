"""
Main exporter implementation for PeakRDL-pybind11
"""
import os
import re
from typing import Optional
from systemrdl.node import RootNode, AddrmapNode, RegNode, RegfileNode, FieldNode
from systemrdl.rdltypes import AccessType, OnReadType, OnWriteType

from jinja2 import Environment, PackageLoader, select_autoescape

class Pybind11Exporter:
    """
    Export SystemRDL register descriptions to PyBind11 C++ modules
    """
    
    def __init__(self):
        self.env = Environment(
            loader=PackageLoader('peakrdl_pybind11', 'templates'),
            autoescape=select_autoescape(),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.soc_name = None
        self.top_node = None
        self.output_dir = None
        
    def export(self, top_node: RootNode, output_dir: str, 
               soc_name: Optional[str] = None, 
               gen_pyi: bool = True):
        """
        Export SystemRDL to PyBind11 modules
        
        Parameters:
            top_node: Root node of the SystemRDL compilation
            output_dir: Directory to write output files
            soc_name: Name of the SoC module (default: derived from top node)
            gen_pyi: Generate .pyi stub files for type hints
        """
        self.top_node = top_node.top if isinstance(top_node, RootNode) else top_node
        self.output_dir = output_dir
        self.soc_name = soc_name or self.top_node.inst_name or "soc"
        
        # Sanitize soc_name for use as identifier
        self.soc_name = self._sanitize_identifier(self.soc_name)
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate C++ descriptor header
        self._generate_descriptors()
        
        # Generate PyBind11 bindings
        self._generate_bindings()
        
        # Generate Python runtime
        self._generate_python_runtime()
        
        # Generate setup.py for building the module
        self._generate_setup_py()
        
        # Generate .pyi stub files if requested
        if gen_pyi:
            self._generate_pyi_stubs()
    
    def _sanitize_identifier(self, name: str) -> str:
        """Sanitize a name to be a valid Python/C++ identifier"""
        # Replace invalid characters with underscores
        name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        # Ensure it doesn't start with a digit
        if name and name[0].isdigit():
            name = '_' + name
        return name or 'soc'
    
    def _generate_descriptors(self):
        """Generate C++ descriptor header file"""
        template = self.env.get_template('descriptors.hpp.jinja')
        
        # Collect all nodes for descriptor generation
        nodes = self._collect_nodes(self.top_node)
        
        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )
        
        filepath = os.path.join(self.output_dir, f'{self.soc_name}_descriptors.hpp')
        with open(filepath, 'w') as f:
            f.write(output)
    
    def _generate_bindings(self):
        """Generate PyBind11 bindings C++ file"""
        template = self.env.get_template('bindings.cpp.jinja')
        
        # Collect all nodes for binding generation
        nodes = self._collect_nodes(self.top_node)
        
        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )
        
        filepath = os.path.join(self.output_dir, f'{self.soc_name}_bindings.cpp')
        with open(filepath, 'w') as f:
            f.write(output)
    
    def _generate_python_runtime(self):
        """Generate Python runtime module"""
        template = self.env.get_template('runtime.py.jinja')
        
        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
        )
        
        filepath = os.path.join(self.output_dir, '__init__.py')
        with open(filepath, 'w') as f:
            f.write(output)
    
    def _generate_setup_py(self):
        """Generate setup.py for building the C++ extension"""
        template = self.env.get_template('setup.py.jinja')
        
        output = template.render(
            soc_name=self.soc_name,
        )
        
        filepath = os.path.join(self.output_dir, 'setup.py')
        with open(filepath, 'w') as f:
            f.write(output)
    
    def _generate_pyi_stubs(self):
        """Generate .pyi stub files for type hints"""
        template = self.env.get_template('stubs.pyi.jinja')
        
        # Collect all nodes for stub generation
        nodes = self._collect_nodes(self.top_node)
        
        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )
        
        filepath = os.path.join(self.output_dir, '__init__.pyi')
        with open(filepath, 'w') as f:
            f.write(output)
    
    def _collect_nodes(self, node, nodes=None):
        """Recursively collect all nodes in the hierarchy"""
        if nodes is None:
            nodes = {
                'addrmaps': [],
                'regfiles': [],
                'regs': [],
                'fields': [],
            }
        
        if isinstance(node, AddrmapNode):
            nodes['addrmaps'].append(node)
            for child in node.children():
                self._collect_nodes(child, nodes)
        elif isinstance(node, RegfileNode):
            nodes['regfiles'].append(node)
            for child in node.children():
                self._collect_nodes(child, nodes)
        elif isinstance(node, RegNode):
            nodes['regs'].append(node)
            for field in node.fields():
                nodes['fields'].append(field)
        
        return nodes
