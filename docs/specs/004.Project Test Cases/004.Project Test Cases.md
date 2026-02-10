# Project test cases

## Description
Each project has many test cases, and this test cases has some meta data that is specified on example file

## Requirements
- Our main focus here is to integrate with testrails, so we can import an exported test run from testrails (an example is presented beside this file: test cases example.xml)
- Each test case, should have exact items of the test case on the example file
- User should be able to edit each test case
- User should be able to delete each test case
- We DO NOT want import file functionality on this step

## Constraints
- Check for access, if a user doesn't have access to a project, it should not be able to interact with test cases (I suggest to create a re-usable function, middleware, or decorator to check if user has access to a project, cause we have lot to do with that later)
- Project test cases must have pagination
- We should be able to search through test cases
- Editing and adding test cases should be handled through modal
- DO NOT work on upload yet, the sample I provided is just a sample for checking the db fields