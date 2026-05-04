use anyhow::{Context, Result};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use crate::Mesh;
pub(crate) fn export_split_variants(meshes: &[Mesh], output_dir: &Path) -> Result<Vec<PathBuf>> {
    fs::create_dir_all(output_dir)
        .with_context(|| format!("Failed to create output dir {}", output_dir.display()))?;

    let mut groups: BTreeMap<String, Vec<Mesh>> = BTreeMap::new();
    for m in meshes {
        groups.entry(m.variant.clone()).or_default().push(m.clone());
    }

    let mut out_files = Vec::new();
    for (variant, group) in groups {
        let out = output_dir.join(format!("{}.obj", variant));
        export_obj(&group, &out)?;
        out_files.push(out);
    }
    Ok(out_files)
}

fn export_obj(meshes: &[Mesh], out: &Path) -> Result<()> {
    let mut s = String::new();
    s.push_str("# Converted from Dagor GRP resources\n");
    s.push_str(&format!("# {} mesh objects\n\n", meshes.len()));

    let mut v_offset: u32 = 1;
    for m in meshes {
        s.push_str(&format!("o {}\n", m.name));
        s.push_str(&format!(
            "# Vertices: {}, Faces: {}, Lines: {}\n",
            m.vertices.len(),
            m.faces.len(),
            m.lines.len()
        ));

        for (x, y, z) in &m.vertices {
            s.push_str(&format!("v {} {} {}\n", x, y, z));
        }
        for (a, b, c) in &m.faces {
            s.push_str(&format!("f {} {} {}\n", a + v_offset, b + v_offset, c + v_offset));
        }
        for (a, b) in &m.lines {
            s.push_str(&format!("l {} {}\n", a + v_offset, b + v_offset));
        }
        s.push('\n');
        v_offset += m.vertices.len() as u32;
    }

    fs::write(out, s).with_context(|| format!("Failed to write {}", out.display()))?;
    Ok(())
}


