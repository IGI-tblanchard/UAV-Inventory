"""One-shot smoke test for ExportMosaicDatasetPaths using ArcGIS Pro Python.

This test creates a unique temporary table in the selected mosaic's file
geodatabase, reads it using the same fields/cursor as the nightly workflow,
and deletes the table afterward. It does not modify the mosaic dataset.
"""

from pathlib import Path
import getpass
import sys
import traceback


DEFAULT_MOSAIC = (
    r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Staging.gdb"
    r"\Cenovus_DSM"
)


def main() -> int:
    try:
        import arcpy
    except Exception:
        print("FAIL: Could not import ArcPy in this Python environment.")
        traceback.print_exc()
        return 2

    mosaic_dataset = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MOSAIC
    mosaic_path = Path(mosaic_dataset)
    catalog_path = mosaic_path.parent / f"AMD_{mosaic_path.name}_CAT"

    print(f"Windows account: {getpass.getuser()}")
    print(f"Python executable: {sys.executable}")
    print(f"ArcGIS Pro version: {arcpy.GetInstallInfo().get('Version', 'unknown')}")
    print(f"Mosaic dataset: {mosaic_dataset}")

    # Match the ArcPy environment configured by the nightly mosaic scripts.
    arcpy.env.overwriteOutput = True

    if not arcpy.Exists(mosaic_dataset):
        print(f"FAIL: Mosaic dataset is not accessible: {mosaic_dataset}")
        return 2
    if not arcpy.Exists(str(catalog_path)):
        print(f"FAIL: Mosaic footprint catalog is not accessible: {catalog_path}")
        return 2

    # Probe ordinary table creation separately to distinguish general
    # geodatabase write access from an ExportMosaicDatasetPaths-specific failure.
    probe_table = None
    try:
        probe_table = arcpy.CreateUniqueName("uav_write_probe", str(mosaic_path.parent))
        arcpy.management.CreateTable(str(mosaic_path.parent), Path(probe_table).name)
        print(f"PASS: Can create a regular table in the geodatabase: {probe_table}")
    except Exception as probe_error:
        print(f"FAIL: Could not create a regular table in the geodatabase: {probe_error}")
        try:
            geoprocessing_messages = arcpy.GetMessages(2)
            if geoprocessing_messages:
                print(geoprocessing_messages)
        except Exception:
            pass
    finally:
        if probe_table and arcpy.Exists(probe_table):
            try:
                arcpy.management.Delete(probe_table)
                print("Write-probe table deleted.")
            except Exception as cleanup_error:
                print(f"WARNING: Could not delete write-probe table: {cleanup_error}")
                print(f"Probe table may remain at: {probe_table}")

    output_table = None
    result = 0
    try:
        # Use the same workspace and naming pattern as the nightly scripts.
        output_table = arcpy.CreateUniqueName(
            f"uav_paths_{mosaic_path.name}", str(mosaic_path.parent)
        )
        print(f"Temporary output table: {output_table}")

        # This is the single ExportMosaicDatasetPaths call under test.
        arcpy.management.ExportMosaicDatasetPaths(
            in_mosaic_dataset=mosaic_dataset,
            out_table=output_table,
            export_mode="ALL",
            types_of_paths="RASTER",
        )

        if not arcpy.Exists(output_table):
            raise RuntimeError(
                f"Export returned without an error, but the output table is missing: {output_table}"
            )

        output_fields = {
            field.name.casefold(): field.name for field in arcpy.ListFields(output_table)
        }
        source_oid_field = output_fields.get("sourceoid")
        path_field = output_fields.get("path")
        if not source_oid_field or not path_field:
            raise RuntimeError(
                f"Output table is missing SourceOID/Path fields: {output_table}"
            )

        path_count = 0
        with arcpy.da.SearchCursor(output_table, [source_oid_field, path_field]) as cursor:
            for _source_oid, source_path in cursor:
                if source_path:
                    path_count += 1

        print(f"PASS: Export created a readable table with {path_count} source paths.")
    except Exception as error:
        result = 1
        print(f"FAIL: Export or table verification failed: {error}")
        try:
            geoprocessing_messages = arcpy.GetMessages(2)
            if geoprocessing_messages:
                print("ArcGIS geoprocessing error messages:")
                print(geoprocessing_messages)
        except Exception:
            pass
        traceback.print_exc()
    finally:
        if output_table and arcpy.Exists(output_table):
            try:
                arcpy.management.Delete(output_table)
                print("Temporary table deleted.")
            except Exception as cleanup_error:
                result = 1
                print(f"WARNING: Could not delete temporary table: {cleanup_error}")
                print(f"Table may remain at: {output_table}")

    return result


if __name__ == "__main__":
    raise SystemExit(main())
