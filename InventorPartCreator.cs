using System;
using System.Runtime.InteropServices;
using Inventor;

namespace CadApiSamples
{
    internal static class InventorPartCreator
    {
        private const double CircleDiameterMm = 50.0;
        private const double ExtrudeDepthMm = 150.0;

        public static void Main()
        {
            Application inventorApp = null;

            try
            {
                inventorApp = StartInventor(visible: true);

                var partDoc = (PartDocument)inventorApp.Documents.Add(
                    DocumentTypeEnum.kPartDocumentObject,
                    inventorApp.FileManager.GetTemplateFile(DocumentTypeEnum.kPartDocumentObject),
                    true);

                CreateExtrudedCylinder(partDoc, CircleDiameterMm, ExtrudeDepthMm);

                Console.WriteLine($"Part created successfully. Diameter = {CircleDiameterMm} mm, Depth = {ExtrudeDepthMm} mm");
            }
            catch (COMException ex)
            {
                Console.Error.WriteLine($"Inventor COM error: {ex.Message}");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"Unexpected error: {ex.Message}");
            }
        }

        private static Application StartInventor(bool visible)
        {
            Application inventorApp;

            try
            {
                inventorApp = (Application)Marshal.GetActiveObject("Inventor.Application");
            }
            catch (COMException)
            {
                var inventorType = Type.GetTypeFromProgID("Inventor.Application")
                    ?? throw new InvalidOperationException("Inventor is not installed or not registered.");

                inventorApp = (Application)Activator.CreateInstance(inventorType)
                    ?? throw new InvalidOperationException("Failed to launch Autodesk Inventor.");
            }

            inventorApp.Visible = visible;
            return inventorApp;
        }

        private static void CreateExtrudedCylinder(PartDocument partDoc, double diameterMm, double depthMm)
        {
            // Inventor internal length units are centimeters.
            double radiusCm = (diameterMm / 2.0) / 10.0;
            double depthCm = depthMm / 10.0;

            PartComponentDefinition partDefinition = partDoc.ComponentDefinition;

            PlanarSketch sketch = partDefinition.Sketches.Add(partDefinition.WorkPlanes[3]);
            TransientGeometry tg = partDoc.Parent.TransientGeometry;

            sketch.SketchCircles.AddByCenterRadius(tg.CreatePoint2d(0, 0), radiusCm);

            Profile profile = sketch.Profiles.AddForSolid();

            ExtrudeDefinition extrudeDefinition = partDefinition.Features.ExtrudeFeatures
                .CreateExtrudeDefinition(profile, PartFeatureOperationEnum.kJoinOperation);

            extrudeDefinition.SetDistanceExtent(depthCm, PartFeatureExtentDirectionEnum.kPositiveExtentDirection);
            partDefinition.Features.ExtrudeFeatures.Add(extrudeDefinition);
        }
    }
}
