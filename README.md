# Inventor C# Example

This repository contains a C# sample that:

1. Opens Autodesk Inventor (or attaches to a running instance)
2. Creates a new part document
3. Sketches a circle with **50 mm diameter**
4. Extrudes it to **150 mm depth**

## File

- `InventorPartCreator.cs`

## Requirements

- Autodesk Inventor installed
- A C# project that references **Autodesk Inventor Object Library** (`Inventor.Interop`)

## Usage

Add `InventorPartCreator.cs` into your C# project and run `Main()`.
