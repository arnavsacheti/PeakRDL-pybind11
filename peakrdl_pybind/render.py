"""Rendering utilities for the PyBind backend."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import List

from .ir import BlockIR, SoCIR


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def render_soc(soc: SoCIR, out_dir: Path) -> None:
    _write(out_dir / "pyproject.toml", _render_pyproject(soc))
    _write(out_dir / "CMakeLists.txt", _render_cmake(soc))

    cpp_dir = out_dir / "cpp"
    _write(cpp_dir / "master.hpp", _render_master_hpp(soc))
    _write(cpp_dir / "master.cpp", _render_master_cpp(soc))
    _write(cpp_dir / "accessors.hpp", _render_accessors_hpp(soc))
    _write(cpp_dir / "reg_model.hpp", _render_reg_model_hpp(soc))
    _write(cpp_dir / "reg_model.cpp", _render_reg_model_cpp(soc))
    _write(cpp_dir / "soc_module.cpp", _render_soc_module_cpp(soc))

    if soc.generate_pyi:
        _write((out_dir / "typing") / f"{soc.module_name}.pyi", _render_typing_stub())

    if soc.include_examples:
        masters_dir = out_dir / "masters"
        _write(masters_dir / "openocd_master.cpp", _render_openocd_example(soc))
        _write(masters_dir / "ssh_devmem_master.cpp", _render_ssh_example(soc))


def _render_pyproject(soc: SoCIR) -> str:
    return dedent(
        f"""
        [build-system]
        requires = ["scikit-build-core>=0.5", "pybind11>=2.10"]
        build-backend = "scikit_build_core.build"

        [project]
        name = "{soc.module_name}"
        version = "0.1.0"
        description = "Auto-generated PyBind11 bindings for {soc.module_name}"
        requires-python = ">=3.9"
        readme = "README.md"
        authors = [{{ name = "Generated" }}]
        license = {{ text = "MIT" }}

        [tool.scikit-build]
        build-dir = "_skbuild"

        [tool.scikit-build.cmake]
        minimum-version = "3.20"
        source-dir = "."

        [tool.scikit-build.wheel]
        expand-macos-universal-tags = true

        [project.optional-dependencies]
        develop = ["pytest"]
        """
    ).strip()


def _render_cmake(soc: SoCIR) -> str:
    return dedent(
        f"""
        cmake_minimum_required(VERSION 3.20)
        project({soc.module_name} LANGUAGES CXX)

        find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
        find_package(pybind11 CONFIG REQUIRED)

        set(CMAKE_CXX_STANDARD 17)
        set(CMAKE_CXX_STANDARD_REQUIRED ON)
        set(CMAKE_POSITION_INDEPENDENT_CODE ON)

        add_library({soc.module_name}_objects OBJECT
            cpp/master.cpp
            cpp/reg_model.cpp
            cpp/soc_module.cpp
        )

        target_include_directories({soc.module_name}_objects PRIVATE cpp)

        pybind11_add_module({soc.module_name} MODULE $<TARGET_OBJECTS:{soc.module_name}_objects>)

        target_include_directories({soc.module_name} PRIVATE cpp)
        target_link_libraries({soc.module_name} PRIVATE pybind11::module)

        target_compile_definitions({soc.module_name} PRIVATE
            SOC_WORD_BYTES={soc.word_bytes}
            SOC_LITTLE_ENDIAN={1 if soc.little_endian else 0}
            SOC_ACCESS_CHECKS={0 if soc.options.get('no_access_checks') else 1}
        )
        """
    ).strip()


def _render_master_hpp(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #pragma once

        #include <cstddef>
        #include <cstdint>
        #include <memory>
        #include <string>

        namespace {ns} {{

        class Master {{
        public:
          virtual ~Master() = default;

          virtual std::uint32_t read32(std::uint64_t addr) = 0;
          virtual void write32(std::uint64_t addr, std::uint32_t data, std::uint32_t wstrb = 0xF) = 0;

          virtual void read_block(std::uint64_t addr, std::uint8_t* dst, std::size_t len);
          virtual void write_block(std::uint64_t addr, const std::uint8_t* src, std::size_t len);

          virtual bool little_endian() const {{ return true; }}
          virtual unsigned word_bytes() const {{ return 4; }}
        }};

        }}  // namespace {ns}
        """
    ).strip()


def _render_master_cpp(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #include "master.hpp"

        namespace {ns} {{

        void Master::read_block(std::uint64_t addr, std::uint8_t* dst, std::size_t len) {{
          const unsigned step = word_bytes();
          for (std::size_t offset = 0; offset < len; offset += step) {{
            std::uint32_t value = read32(addr + offset);
            for (unsigned i = 0; i < step && offset + i < len; ++i) {{
              dst[offset + i] = static_cast<std::uint8_t>((value >> (8 * i)) & 0xFFu);
            }}
          }}
        }}

        void Master::write_block(std::uint64_t addr, const std::uint8_t* src, std::size_t len) {{
          const unsigned step = word_bytes();
          for (std::size_t offset = 0; offset < len; offset += step) {{
            std::uint32_t value = 0;
            std::uint32_t mask = 0;
            for (unsigned i = 0; i < step && offset + i < len; ++i) {{
              value |= static_cast<std::uint32_t>(src[offset + i]) << (8 * i);
              mask |= (1u << i);
            }}
            write32(addr + offset, value, mask);
          }}
        }}

        }}  // namespace {ns}
        """
    ).strip()


def _render_accessors_hpp(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #pragma once

        #include <cstdint>
        #include <stdexcept>
        #include <string>

        namespace {ns} {{

        enum class AccessMode : std::uint8_t {{
          kRW,
          kRO,
          kWO,
          kW1C,
          kW0C,
          kRC
        }};

        class AccessError : public std::runtime_error {{
        public:
          explicit AccessError(const std::string& what_arg) : std::runtime_error(what_arg) {{}}
        }};

        inline std::uint32_t mask_width(std::uint32_t width) {{
          if (width >= 32) {{
            return 0xFFFF'FFFFu;
          }}
          return (static_cast<std::uint32_t>(1u) << width) - 1u;
        }}

        inline std::uint32_t apply_access_policy(AccessMode mode, std::uint32_t value, std::uint32_t existing) {{
          switch (mode) {{
            case AccessMode::kRW:
              return value;
            case AccessMode::kRO:
              return existing;
            case AccessMode::kWO:
              return value;
            case AccessMode::kW1C:
              return value;
            case AccessMode::kW0C:
              return value;
            case AccessMode::kRC:
              return existing;
          }}
          return value;
        }}

        inline bool write_requires_existing(AccessMode mode) {{
          return mode == AccessMode::kRW;
        }}

        }}  // namespace {ns}
        """
    ).strip()


def _render_reg_model_hpp(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #pragma once

        #include <cstddef>
        #include <cstdint>
        #include <memory>
        #include <string>

        #include <pybind11/pybind11.h>

        #include "master.hpp"
        #include "accessors.hpp"

        namespace {ns} {{

        struct FieldDesc {{
          const char* name;
          std::uint8_t lsb;
          std::uint8_t msb;
          AccessMode access;
          std::uint32_t reset;
        }};

        struct RegDesc {{
          const char* name;
          const char* path;
          std::uint64_t address;
          std::uint32_t offset;
          std::uint16_t width;
          std::uint32_t reset;
          bool is_volatile;
          AccessMode access;
          const FieldDesc* fields;
          std::size_t field_count;
        }};

        struct ChildDesc {{
          const char* name;
          const struct BlockDesc* desc;
          std::uint64_t offset;
        }};

        struct ArrayDesc {{
          const char* name;
          const struct BlockDesc* element;
          std::size_t count;
          std::uint64_t stride;
        }};

        struct BlockDesc {{
          const char* name;
          const char* path;
          std::uint64_t base_address;
          const RegDesc* regs;
          std::size_t reg_count;
          const ChildDesc* blocks;
          std::size_t block_count;
          const ArrayDesc* arrays;
          std::size_t array_count;
        }};

        class FieldProxy {{
        public:
          FieldProxy(const FieldDesc* desc, const RegDesc* reg, std::shared_ptr<Master>* master, std::uint64_t base);

          std::uint32_t get() const;
          void set(std::uint32_t value) const;
          std::uint8_t lsb() const;
          std::uint8_t msb() const;
          std::uint32_t mask() const;
          const char* name() const;
          const char* path() const;

        private:
          const FieldDesc* desc_;
          const RegDesc* reg_;
          std::shared_ptr<Master>* master_;
          std::uint64_t base_;
        }};

        class RegProxy {{
        public:
          RegProxy(const RegDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base);

          std::uint32_t read() const;
          void write(std::uint32_t value) const;
          void set_bits(std::uint32_t mask) const;
          void clear_bits(std::uint32_t mask) const;
          void modify(pybind11::kwargs kwargs) const;
          std::uint64_t address() const;
          std::uint16_t width() const;
          const char* name() const;
          const char* path() const;
          pybind11::object getattr(const std::string& name) const;

        private:
          const RegDesc* desc_;
          std::shared_ptr<Master>* master_;
          std::uint64_t base_;
        }};

        class ArrayProxy;

        class BlockProxy {{
        public:
          BlockProxy(const BlockDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base);

          pybind11::object getattr(const std::string& name) const;
          std::string path() const;
          std::uint64_t base_address() const;

        private:
          const BlockDesc* desc_;
          std::shared_ptr<Master>* master_;
          std::uint64_t base_;
        }};

        class ArrayProxy {{
        public:
          ArrayProxy(const ArrayDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base);
          pybind11::object get_item(std::size_t index) const;
          std::size_t size() const;

        private:
          const ArrayDesc* desc_;
          std::shared_ptr<Master>* master_;
          std::uint64_t base_;
        }};

        std::shared_ptr<BlockProxy> make_block_proxy(const BlockDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base);

        void bind_reg_model(pybind11::module_& m, std::shared_ptr<Master>* master);

        void reset_to_defaults(std::shared_ptr<Master>* master);

        }}  // namespace {ns}
        """
    ).strip()


def _render_reg_model_cpp(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    lines: List[str] = []
    lines.append('#include "reg_model.hpp"\n')
    lines.append("#include <algorithm>\n#include <cstddef>\n#include <cstdint>\n#include <memory>\n#include <sstream>\n#include <stdexcept>\n#include <string>\n#include <utility>\n#include <vector>\n")
    lines.append("#include <pybind11/stl.h>\n\n")
    lines.append(f"namespace {ns} {{\n\n")
    lines.append("namespace {\n")
    lines.append("using pybind11::kwargs;\nusing pybind11::module_;\nusing pybind11::object;\nusing pybind11::str;\nusing pybind11::cast;\n\n")
    lines.append(
        "std::shared_ptr<Master>& require_master(std::shared_ptr<Master>* master_ptr) {\n"
        "  if (!master_ptr || !*master_ptr) {\n"
        "    throw std::runtime_error(\"No master attached\");\n"
        "  }\n  return *master_ptr;\n}\n\n"
    )
    lines.append("std::uint64_t effective_address(const RegDesc* desc, std::uint64_t base) {\n  return base + static_cast<std::uint64_t>(desc->offset);\n}\n\n")

    rendered: set[int] = set()

    def emit_block(block: BlockIR) -> None:
        if id(block) in rendered:
            return
        rendered.add(id(block))

        for child in block.blocks:
            emit_block(child)
        for array in block.arrays:
            emit_block(array.element)

        for reg in block.registers:
            if not reg.fields:
                continue
            lines.append(f"static const FieldDesc {reg.to_cpp_name()}_fields[] = {{\n")
            for field in reg.fields:
                reset_value = field.reset if field.reset is not None else 0
                lines.append(
                    f"  {{\"{field.name}\", {field.lsb}, {field.msb}, {field.access.to_cpp()}, {reset_value}u}}\n"
                )
            lines.append("};\n\n")

        if block.registers:
            lines.append(f"static const RegDesc {block.to_cpp_name()}_regs[] = {{\n")
            for reg in block.registers:
                reset_value = reg.reset if reg.reset is not None else 0
                fields_ptr = f"{reg.to_cpp_name()}_fields" if reg.fields else "nullptr"
                field_count = len(reg.fields) if reg.fields else 0
                lines.append(
                    "  {\n"
                    f"    \"{reg.name}\",\n"
                    f"    \"{reg.path}\",\n"
                    f"    {reg.address}ULL,\n"
                    f"    {reg.offset}u,\n"
                    f"    {reg.width},\n"
                    f"    {reset_value}u,\n"
                    f"    {'true' if reg.is_volatile else 'false'},\n"
                    f"    {reg.access.to_cpp()},\n"
                    f"    {fields_ptr},\n"
                    f"    {field_count}\n"
                    "  },\n"
                )
            lines.append("};\n\n")

        if block.blocks:
            lines.append(f"static const ChildDesc {block.to_cpp_name()}_children[] = {{\n")
            for child in block.blocks:
                offset = child.base_address - block.base_address
                lines.append(f"  {{\"{child.name}\", &{child.to_cpp_name()}_desc, {offset}ULL}}\n")
            lines.append("};\n\n")

        if block.arrays:
            lines.append(f"static const ArrayDesc {block.to_cpp_name()}_arrays[] = {{\n")
            for array in block.arrays:
                lines.append(
                    f"  {{\"{array.name}\", &{array.element.to_cpp_name()}_desc, {array.count}, {array.stride}ULL}}\n"
                )
            lines.append("};\n\n")

        reg_ptr = f"{block.to_cpp_name()}_regs" if block.registers else "nullptr"
        reg_count = len(block.registers)
        child_ptr = f"{block.to_cpp_name()}_children" if block.blocks else "nullptr"
        child_count = len(block.blocks)
        array_ptr = f"{block.to_cpp_name()}_arrays" if block.arrays else "nullptr"
        array_count = len(block.arrays)

        lines.append(
            f"static const BlockDesc {block.to_cpp_name()}_desc = {{\n"
            f"  \"{block.name}\",\n"
            f"  \"{block.path}\",\n"
            f"  {block.base_address}ULL,\n"
            f"  {reg_ptr},\n"
            f"  {reg_count},\n"
            f"  {child_ptr},\n"
            f"  {child_count},\n"
            f"  {array_ptr},\n"
            f"  {array_count}\n"
            "};\n\n"
        )

    emit_block(soc.top)

    lines.append(f"const BlockDesc* const kTopBlock = &{soc.top.to_cpp_name()}_desc;\n\n")
    lines.append(
        "std::vector<const RegDesc*> collect_all_registers() {\n"
        "  static const std::vector<const RegDesc*> regs = [] {\n"
        "    std::vector<const RegDesc*> result;\n"
        "    std::vector<const BlockDesc*> stack = {kTopBlock};\n"
        "    while (!stack.empty()) {\n"
        "      const BlockDesc* current = stack.back();\n"
        "      stack.pop_back();\n"
        "      for (std::size_t i = 0; i < current->reg_count; ++i) {\n"
        "        result.push_back(&current->regs[i]);\n"
        "      }\n"
        "      for (std::size_t i = 0; i < current->block_count; ++i) {\n"
        "        stack.push_back(current->blocks[i].desc);\n"
        "      }\n"
        "      for (std::size_t i = 0; i < current->array_count; ++i) {\n"
        "        stack.push_back(current->arrays[i].element);\n"
        "      }\n"
        "    }\n"
        "    return result;\n"
        "  }();\n"
        "  return regs;\n"
        "}\n\n"
    )
    lines.append("}  // namespace\n\n")

    lines.append(
        "FieldProxy::FieldProxy(const FieldDesc* desc, const RegDesc* reg, std::shared_ptr<Master>* master, std::uint64_t base)\n"
        "    : desc_(desc), reg_(reg), master_(master), base_(base) {}\n\n"
    )
    lines.append(
        "std::uint32_t FieldProxy::get() const {\n"
        "  RegProxy reg(reg_, master_, base_);\n"
        "  std::uint32_t value = reg.read();\n"
        "  std::uint32_t mask = mask_width(static_cast<std::uint32_t>(desc_->msb - desc_->lsb + 1));\n"
        "  return (value >> desc_->lsb) & mask;\n"
        "}\n\n"
    )
    lines.append(
        "void FieldProxy::set(std::uint32_t value) const {\n"
        "  RegProxy reg(reg_, master_, base_);\n"
        "  std::uint32_t mask = mask_width(static_cast<std::uint32_t>(desc_->msb - desc_->lsb + 1)) << desc_->lsb;\n"
        "  std::uint32_t current = reg.read();\n"
        "  std::uint32_t new_value = (current & ~mask) | ((value << desc_->lsb) & mask);\n"
        "  reg.write(new_value);\n"
        "}\n\n"
    )
    lines.extend(
        [
            "std::uint8_t FieldProxy::lsb() const { return desc_->lsb; }\n",
            "std::uint8_t FieldProxy::msb() const { return desc_->msb; }\n",
            "std::uint32_t FieldProxy::mask() const { return mask_width(static_cast<std::uint32_t>(desc_->msb - desc_->lsb + 1)) << desc_->lsb; }\n",
            "const char* FieldProxy::name() const { return desc_->name; }\n",
            "const char* FieldProxy::path() const { return reg_->path; }\n\n",
        ]
    )
    lines.append(
        "RegProxy::RegProxy(const RegDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base)\n"
        "    : desc_(desc), master_(master), base_(base) {}\n\n"
    )
    lines.append(
        "std::uint32_t RegProxy::read() const {\n"
        "  auto& master = require_master(master_);\n"
        "  pybind11::gil_scoped_release nogil;\n"
        "  return master->read32(effective_address(desc_, base_));\n"
        "}\n\n"
    )
    lines.append(
        "void RegProxy::write(std::uint32_t value) const {\n"
        "#if SOC_ACCESS_CHECKS\n"
        "  if (desc_->access == AccessMode::kRO) {\n"
        "    throw AccessError(std::string(\"Register is read-only: \") + desc_->path);\n"
        "  }\n"
        "#endif\n"
        "  std::uint32_t width_mask = mask_width(desc_->width);\n"
        "  value &= width_mask;\n"
        "  auto& master = require_master(master_);\n"
        "  std::uint32_t existing = 0;\n"
        "  if (write_requires_existing(desc_->access)) {\n"
        "    pybind11::gil_scoped_release nogil;\n"
        "    existing = master->read32(effective_address(desc_, base_));\n"
        "  }\n"
        "  std::uint32_t write_value = apply_access_policy(desc_->access, value, existing);\n"
        "  unsigned byte_count = (desc_->width + 7u) / 8u;\n"
        "  std::uint32_t wstrb = byte_count >= 4 ? 0xFu : ((1u << byte_count) - 1u);\n"
        "  pybind11::gil_scoped_release nogil;\n"
        "  master->write32(effective_address(desc_, base_), write_value, wstrb);\n"
        "}\n\n"
    )
    lines.extend(
        [
            "void RegProxy::set_bits(std::uint32_t mask) const {\n  std::uint32_t value = read();\n  write(value | mask);\n}\n\n",
            "void RegProxy::clear_bits(std::uint32_t mask) const {\n  std::uint32_t value = read();\n  write(value & ~mask);\n}\n\n",
        ]
    )
    lines.append(
        "void RegProxy::modify(pybind11::kwargs kwargs) const {\n"
        "  std::uint32_t value = read();\n"
        "  for (auto& item : kwargs) {\n"
        "    std::string key = pybind11::cast<std::string>(item.first);\n"
        "    bool matched = false;\n"
        "    for (std::size_t idx = 0; idx < desc_->field_count; ++idx) {\n"
        "      const FieldDesc& field = desc_->fields[idx];\n"
        "      if (key == field.name) {\n"
        "        std::uint32_t field_mask = mask_width(static_cast<std::uint32_t>(field.msb - field.lsb + 1)) << field.lsb;\n"
        "        std::uint32_t field_value = pybind11::cast<std::uint32_t>(item.second);\n"
        "        value = (value & ~field_mask) | ((field_value << field.lsb) & field_mask);\n"
        "        matched = true;\n"
        "        break;\n"
        "      }\n"
        "    }\n"
        "    if (!matched) {\n"
        "      throw std::runtime_error(\"Unknown field: \" + key);\n"
        "    }\n"
        "  }\n"
        "  write(value);\n"
        "}\n\n"
    )
    lines.extend(
        [
            "std::uint64_t RegProxy::address() const { return effective_address(desc_, base_); }\n",
            "std::uint16_t RegProxy::width() const { return desc_->width; }\n",
            "const char* RegProxy::name() const { return desc_->name; }\n",
            "const char* RegProxy::path() const { return desc_->path; }\n\n",
        ]
    )
    lines.append(
        "pybind11::object RegProxy::getattr(const std::string& name) const {\n"
        "  for (std::size_t i = 0; i < desc_->field_count; ++i) {\n"
        "    if (name == desc_->fields[i].name) {\n"
        "      return pybind11::cast(std::make_shared<FieldProxy>(&desc_->fields[i], desc_, master_, base_));\n"
        "    }\n"
        "  }\n"
        "  throw pybind11::attribute_error(\"Unknown field: \" + name);\n"
        "}\n\n"
    )
    lines.append(
        "BlockProxy::BlockProxy(const BlockDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base)\n"
        "    : desc_(desc), master_(master), base_(base) {}\n\n"
    )
    lines.append(
        "pybind11::object BlockProxy::getattr(const std::string& name) const {\n"
        "  for (std::size_t i = 0; i < desc_->reg_count; ++i) {\n"
        "    const RegDesc* reg = &desc_->regs[i];\n"
        "    if (name == reg->name) {\n"
        "      return pybind11::cast(std::make_shared<RegProxy>(reg, master_, base_));\n"
        "    }\n"
        "  }\n"
        "  for (std::size_t i = 0; i < desc_->block_count; ++i) {\n"
        "    const ChildDesc& child = desc_->blocks[i];\n"
        "    if (name == child.name) {\n"
        "      std::uint64_t child_base = base_ + child.offset;\n"
        "      return pybind11::cast(std::make_shared<BlockProxy>(child.desc, master_, child_base));\n"
        "    }\n"
        "  }\n"
        "  for (std::size_t i = 0; i < desc_->array_count; ++i) {\n"
        "    const ArrayDesc& array = desc_->arrays[i];\n"
        "    if (name == array.name) {\n"
        "      return pybind11::cast(std::make_shared<ArrayProxy>(&array, master_, base_));\n"
        "    }\n"
        "  }\n"
        "  throw pybind11::attribute_error(\"Unknown register or block: \" + name);\n"
        "}\n\n"
    )
    lines.extend(
        [
            "std::string BlockProxy::path() const { return desc_->path; }\n",
            "std::uint64_t BlockProxy::base_address() const { return base_; }\n\n",
        ]
    )
    lines.append(
        "ArrayProxy::ArrayProxy(const ArrayDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base)\n"
        "    : desc_(desc), master_(master), base_(base) {}\n\n"
    )
    lines.append(
        "pybind11::object ArrayProxy::get_item(std::size_t index) const {\n"
        "  if (index >= desc_->count) {\n"
        "    throw pybind11::index_error(\"Index out of range\");\n"
        "  }\n"
        "  std::uint64_t element_base = base_ + static_cast<std::uint64_t>(index) * desc_->stride;\n"
        "  return pybind11::cast(std::make_shared<BlockProxy>(desc_->element, master_, element_base));\n"
        "}\n\n"
    )
    lines.append("std::size_t ArrayProxy::size() const { return desc_->count; }\n\n")
    lines.append(
        "std::shared_ptr<BlockProxy> make_block_proxy(const BlockDesc* desc, std::shared_ptr<Master>* master, std::uint64_t base) {\n"
        "  return std::make_shared<BlockProxy>(desc, master, base);\n"
        "}\n\n"
    )
    if soc.options.get("emit_reset_writes", True):
        lines.append(
            "void reset_to_defaults(std::shared_ptr<Master>* master) {\n"
            "  auto regs = collect_all_registers();\n"
            "  for (const RegDesc* reg : regs) {\n"
            "    if (reg->access == AccessMode::kRO) {\n"
            "      continue;\n"
            "    }\n"
            "    if (reg->reset != 0 || reg->field_count > 0) {\n"
            "      RegProxy proxy(reg, master, reg->address - reg->offset);\n"
            "      proxy.write(reg->reset);\n"
            "    }\n"
            "  }\n"
            "}\n\n"
        )
    else:
        lines.append("void reset_to_defaults(std::shared_ptr<Master>* master) { (void)master; }\n\n")
    lines.append("void bind_reg_model(pybind11::module_& m, std::shared_ptr<Master>* master) {\n  namespace py = pybind11;\n\n")
    lines.append(
        "  py::class_<FieldProxy, std::shared_ptr<FieldProxy>>(m, \"Field\")\n"
        "      .def(\"get\", &FieldProxy::get)\n"
        "      .def(\"set\", &FieldProxy::set)\n"
        "      .def_property_readonly(\"lsb\", &FieldProxy::lsb)\n"
        "      .def_property_readonly(\"msb\", &FieldProxy::msb)\n"
        "      .def_property_readonly(\"mask\", &FieldProxy::mask)\n"
        "      .def_property_readonly(\"name\", &FieldProxy::name)\n"
        "      .def_property_readonly(\"path\", &FieldProxy::path);\n\n"
    )
    lines.append(
        "  py::class_<RegProxy, std::shared_ptr<RegProxy>>(m, \"Register\")\n"
        "      .def(\"read\", &RegProxy::read)\n"
        "      .def(\"write\", &RegProxy::write, py::arg(\"value\"))\n"
        "      .def(\"set_bits\", &RegProxy::set_bits, py::arg(\"mask\"))\n"
        "      .def(\"clear_bits\", &RegProxy::clear_bits, py::arg(\"mask\"))\n"
        "      .def(\"modify\", &RegProxy::modify)\n"
        "      .def_property_readonly(\"address\", &RegProxy::address)\n"
        "      .def_property_readonly(\"width\", &RegProxy::width)\n"
        "      .def_property_readonly(\"name\", &RegProxy::name)\n"
        "      .def_property_readonly(\"path\", &RegProxy::path)\n"
        "      .def(\"__getattr__\", &RegProxy::getattr);\n\n"
    )
    lines.append(
        "  py::class_<ArrayProxy, std::shared_ptr<ArrayProxy>>(m, \"Array\")\n"
        "      .def(\"__getitem__\", &ArrayProxy::get_item)\n"
        "      .def_property_readonly(\"size\", &ArrayProxy::size);\n\n"
    )
    lines.append(
        "  py::class_<BlockProxy, std::shared_ptr<BlockProxy>>(m, \"Block\")\n"
        "      .def(\"__getattr__\", &BlockProxy::getattr)\n"
        "      .def_property_readonly(\"path\", &BlockProxy::path)\n"
        "      .def_property_readonly(\"base_address\", &BlockProxy::base_address);\n\n"
    )
    lines.append(
        f"  m.attr(\"top\") = py::cast(make_block_proxy(kTopBlock, master, kTopBlock->base_address));\n}}\n\n"
    )
    lines.append(f"}}  // namespace {ns}\n")

    return "".join(lines)


def _render_soc_module_cpp(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #include <pybind11/pybind11.h>
        #include <pybind11/stl.h>

        #include "master.hpp"
        #include "reg_model.hpp"

        namespace py = pybind11;
        namespace soc = {ns};

        static std::shared_ptr<soc::Master> g_master;

        PYBIND11_MODULE({soc.module_name}, m) {{
          m.doc() = "Auto-generated SoC register bindings";

          py::register_exception<soc::AccessError>(m, "AccessError");

          py::class_<soc::Master, std::shared_ptr<soc::Master>>(m, "Master")
              .def(py::init<>())
              .def("read32", &soc::Master::read32)
              .def("write32", &soc::Master::write32, py::arg("addr"), py::arg("data"), py::arg("wstrb") = 0xF)
              .def("read_block", &soc::Master::read_block)
              .def("write_block", &soc::Master::write_block)
              .def_property_readonly("little_endian", &soc::Master::little_endian)
              .def_property_readonly("word_bytes", &soc::Master::word_bytes);

          m.def("attach_master", [](std::shared_ptr<soc::Master> master) {{
            g_master = std::move(master);
          }});

          m.def("master_attached", []() {{ return static_cast<bool>(g_master); }});

          soc::bind_reg_model(m, &g_master);
          m.def("reset_to_defaults", []() {{ soc::reset_to_defaults(&g_master); }});
        }}
        """
    ).strip()


def _render_typing_stub() -> str:
    return dedent(
        """
        from typing import Any

        class Master:
            def read32(self, addr: int) -> int: ...
            def write32(self, addr: int, data: int, wstrb: int = 0xF) -> None: ...
            def read_block(self, addr: int, dst: bytearray, length: int) -> None: ...
            def write_block(self, addr: int, src: bytes, length: int) -> None: ...
            @property
            def little_endian(self) -> bool: ...
            @property
            def word_bytes(self) -> int: ...

        class Field:
            def get(self) -> int: ...
            def set(self, value: int) -> None: ...
            @property
            def lsb(self) -> int: ...
            @property
            def msb(self) -> int: ...
            @property
            def mask(self) -> int: ...
            @property
            def name(self) -> str: ...
            @property
            def path(self) -> str: ...

        class Register:
            def read(self) -> int: ...
            def write(self, value: int) -> None: ...
            def set_bits(self, mask: int) -> None: ...
            def clear_bits(self, mask: int) -> None: ...
            def modify(self, **kwargs: int) -> None: ...
            @property
            def address(self) -> int: ...
            @property
            def width(self) -> int: ...
            @property
            def name(self) -> str: ...
            @property
            def path(self) -> str: ...

        class Block:
            def __getattr__(self, name: str) -> Any: ...
            @property
            def path(self) -> str: ...
            @property
            def base_address(self) -> int: ...

        class Array:
            def __getitem__(self, index: int) -> Block: ...
            @property
            def size(self) -> int: ...

        class AccessError(RuntimeError):
            ...

        def attach_master(master: Master) -> None: ...
        def master_attached() -> bool: ...
        def reset_to_defaults() -> None: ...

        top: Block
        """
    ).strip()


def _render_openocd_example(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #include "master.hpp"

        #include <pybind11/pybind11.h>

        #include <cstdint>
        #include <stdexcept>
        #include <string>

        namespace py = pybind11;
        namespace {ns} {{

        class OpenOCDMaster : public Master {{
        public:
          OpenOCDMaster(std::string host, int port);

          std::uint32_t read32(std::uint64_t addr) override;
          void write32(std::uint64_t addr, std::uint32_t data, std::uint32_t wstrb = 0xF) override;

        private:
          std::string host_;
          int port_;
        }};

        OpenOCDMaster::OpenOCDMaster(std::string host, int port) : host_(std::move(host)), port_(port) {{}}

        std::uint32_t OpenOCDMaster::read32(std::uint64_t addr) {{
          (void)addr;
          throw std::runtime_error("OpenOCD master stub - implement transport");
        }}

        void OpenOCDMaster::write32(std::uint64_t addr, std::uint32_t data, std::uint32_t wstrb) {{
          (void)addr;
          (void)data;
          (void)wstrb;
          throw std::runtime_error("OpenOCD master stub - implement transport");
        }}

        void bind_openocd(py::module_& m) {{
          py::class_<OpenOCDMaster, Master, std::shared_ptr<OpenOCDMaster>>(m, "OpenOCDMaster")
              .def(py::init<std::string, int>(), py::arg("host"), py::arg("port") = 4444);
        }}

        }}  // namespace {ns}
        """
    ).strip()


def _render_ssh_example(soc: SoCIR) -> str:
    ns = soc.module_name.replace("-", "_")
    return dedent(
        f"""
        #include "master.hpp"

        #include <pybind11/pybind11.h>

        #include <cstdint>
        #include <stdexcept>
        #include <string>

        namespace py = pybind11;
        namespace {ns} {{

        class SSHDevmemMaster : public Master {{
        public:
          SSHDevmemMaster(std::string host, std::string user);

          std::uint32_t read32(std::uint64_t addr) override;
          void write32(std::uint64_t addr, std::uint32_t data, std::uint32_t wstrb = 0xF) override;

        private:
          std::string host_;
          std::string user_;
        }};

        SSHDevmemMaster::SSHDevmemMaster(std::string host, std::string user)
            : host_(std::move(host)), user_(std::move(user)) {{}}

        std::uint32_t SSHDevmemMaster::read32(std::uint64_t addr) {{
          (void)addr;
          throw std::runtime_error("SSH devmem master stub - implement transport");
        }}

        void SSHDevmemMaster::write32(std::uint64_t addr, std::uint32_t data, std::uint32_t wstrb) {{
          (void)addr;
          (void)data;
          (void)wstrb;
          throw std::runtime_error("SSH devmem master stub - implement transport");
        }}

        void bind_ssh_devmem(py::module_& m) {{
          py::class_<SSHDevmemMaster, Master, std::shared_ptr<SSHDevmemMaster>>(m, "SSHDevmemMaster")
              .def(py::init<std::string, std::string>(), py::arg("host"), py::arg("user"));
        }}

        }}  // namespace {ns}
        """
    ).strip()
