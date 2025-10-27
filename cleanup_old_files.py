# cleanup_old_files.py
"""
Script to clean up old files after refactoring.
Run this script ONLY after confirming the new structure works correctly.
"""

import os
import shutil

# Files and directories to remove after refactoring
OLD_FILES_TO_REMOVE = [
    "claims_agent.py",
    "claims_prompts.py", 
    "Data_models.py",
    "insurance_config.py",
    "prompt.py",
    "__init__.py",  # This was in root, not needed there
]

OLD_DIRECTORIES_TO_REMOVE = [
    "prompts",
    "routes", 
    "services",
]

def cleanup_old_files():
    """Remove old files and directories after successful refactoring"""
    base_path = os.path.dirname(os.path.abspath(__file__))
    
    print("🧹 Cleaning up old files after refactoring...")
    
    # Remove old files
    for file_name in OLD_FILES_TO_REMOVE:
        file_path = os.path.join(base_path, file_name)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"✅ Removed file: {file_name}")
            except Exception as e:
                print(f"❌ Error removing file {file_name}: {e}")
        else:
            print(f"ℹ️  File not found (already removed?): {file_name}")
    
    # Remove old directories
    for dir_name in OLD_DIRECTORIES_TO_REMOVE:
        dir_path = os.path.join(base_path, dir_name)
        if os.path.exists(dir_path):
            try:
                shutil.rmtree(dir_path)
                print(f"✅ Removed directory: {dir_name}")
            except Exception as e:
                print(f"❌ Error removing directory {dir_name}: {e}")
        else:
            print(f"ℹ️  Directory not found (already removed?): {dir_name}")
    
    print("\n🎉 Cleanup complete!")
    print("\nRemaining files in root directory should be:")
    print("  - main.py (entry point)")
    print("  - requirements.txt")
    print("  - .gitignore")
    print("  - REFACTORING_SUMMARY.md")
    print("  - src/ (all source code)")
    print("  - cleanup_old_files.py (this script - can be removed after use)")

if __name__ == "__main__":
    response = input("Are you sure you want to remove old files? The new structure should be tested first! (y/N): ")
    if response.lower() == 'y':
        cleanup_old_files()
    else:
        print("Cleanup cancelled. Test the new structure first!")