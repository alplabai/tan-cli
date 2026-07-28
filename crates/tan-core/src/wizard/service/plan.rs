// SPDX-License-Identifier: Apache-2.0
//! Module-name normalization and wizard/module-scaffold planning entry points.

use super::c_project::gen_c_project_files;
use super::module_scaffold::gen_module_files;
use super::registry::{MODULE_TEMPLATE_DEFINITIONS, TEMPLATE_DEFINITIONS};
use crate::wizard::models::{
    ModuleScaffoldInput, ModuleScaffoldPlan, WizardPlan, WizardPlanInput, WizardPlannedFile,
};

/// Lowercase `name` and collapse runs of non-`[a-z0-9]` chars into single `_`
/// separators (no leading/trailing). `Err` if nothing remains.
pub fn normalize_module_name(name: &str) -> Result<String, String> {
    let lower = name.trim().to_lowercase();

    // Replace sequences of [^a-z0-9] with a single underscore, no leading/trailing.
    let mut result = String::new();
    let mut in_sep = false;
    for ch in lower.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            if in_sep && !result.is_empty() {
                result.push('_');
            }
            result.push(ch);
            in_sep = false;
        } else {
            in_sep = true;
        }
    }

    if result.is_empty() {
        Err("Module name is empty after normalization.".to_string())
    } else {
        Ok(result)
    }
}

/// Plan the file set for a single-(app-)core project from `input`. Convenience
/// wrapper over `create_wizard_plan_with_cores` with no companion cores.
pub fn create_wizard_plan(input: &WizardPlanInput) -> WizardPlan {
    create_wizard_plan_with_cores(input, &[])
}

/// Like `create_wizard_plan`, but scaffolds a heterogeneous project across the
/// given `(core_id, os)` cores: companion cores + a default RPMsg channel land
/// in board.yaml. An empty slice is the single-(app-)core default.
pub fn create_wizard_plan_with_cores(
    input: &WizardPlanInput,
    cores: &[(String, String)],
) -> WizardPlan {
    let def = TEMPLATE_DEFINITIONS
        .iter()
        .find(|d| d.id == input.template_id)
        .expect("template id must exist in registry");

    let files = gen_c_project_files(def, input.som_sku.as_deref(), cores);

    WizardPlan {
        template_id: input.template_id,
        files,
    }
}

/// Plan the source/header/README file set for a module scaffold. Normalizes the
/// requested module name first; returns `Err` if the name normalizes to empty.
pub fn create_module_scaffold_plan(
    input: &ModuleScaffoldInput,
) -> Result<ModuleScaffoldPlan, String> {
    let normalized_name = normalize_module_name(&input.module_name)?;
    let def = MODULE_TEMPLATE_DEFINITIONS
        .iter()
        .find(|d| d.id == input.template_id)
        .expect("module template id must exist in registry");

    let files = gen_module_files(def, &normalized_name);
    Ok(ModuleScaffoldPlan {
        template_id: input.template_id,
        normalized_name,
        files,
    })
}

/// Render planned files as an ASCII `tree`-style preview, paths sorted
/// lexicographically with the last entry marked by `` `-- ``.
pub fn create_scaffold_tree_preview(files: &[WizardPlannedFile]) -> String {
    let mut paths: Vec<&str> = files.iter().map(|f| f.relative_path.as_str()).collect();
    paths.sort();

    let mut s = String::from(".\n");
    let count = paths.len();
    for (i, path) in paths.iter().enumerate() {
        if i + 1 == count {
            s.push_str(&format!("`-- {path}\n"));
        } else {
            s.push_str(&format!("|-- {path}\n"));
        }
    }
    s
}
